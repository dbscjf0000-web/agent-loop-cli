"""LIVE `agent_loop.run` over MCP subprocess. ~3 minute budget.

Spawns `mcp serve`, sends `initialize`, then `tools/call agent_loop.run`
with a tiny task using cursor for every phase. Reads response and exits.

Run with:
    LD_LIBRARY_PATH=/apps/applications/PYTHON/3.14.2/lib \\
        /apps/applications/PYTHON/3.14.2/bin/python3 tests/e2e_mcp_live_run.py
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path


CONFIG = "/tmp/al_v05_e2e/config.toml"
ROOT = "/tmp/al_v05_e2e/.agent_loop"
TIMEOUT_TOTAL = 600  # 10 minutes hard cap (script-level; tool itself is sync)


def main() -> int:
    print("=" * 78)
    print("LIVE agent_loop.run via MCP subprocess (cursor for every phase)")
    print("=" * 78)
    Path("/tmp/al_v05_e2e").mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "agent_loop.cli", "mcp", "serve",
        "--config", CONFIG,
        "--root", ROOT,
    ]
    print(f"[start] cmd={' '.join(cmd)}")
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=os.environ.copy(),
    )

    def send(method, params=None, rid=None):
        req = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            req["params"] = params
        line = json.dumps(req)
        p.stdin.write(line + "\n")
        p.stdin.flush()
        if rid is None:
            return None
        out = p.stdout.readline()
        return json.loads(out) if out else None

    # 1) initialize (quick)
    r = send("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {}, "clientInfo": {"name": "live-run"}}, rid=1)
    print(f"[init] serverInfo={r.get('result', {}).get('serverInfo')}")

    # 2) live agent_loop.run — short task. We use 1 cycle, 1 max_redo.
    task = "Implement is_even(n: int) -> bool returning True if n is even, False otherwise."
    print(f"[run] task={task!r}")
    print(f"[run] sending tools/call agent_loop.run, this will block until cursor finishes...")
    t0 = time.time()
    req = {
        "jsonrpc": "2.0", "method": "tools/call", "id": 100,
        "params": {
            "name": "agent_loop.run",
            "arguments": {
                "task": task,
                "cycles": 1,
                "max_redo": 1,
                "cross_task": False,
            },
        },
    }
    p.stdin.write(json.dumps(req) + "\n")
    p.stdin.flush()

    # Block until response or timeout, with progress prints from stderr.
    stderr_buf: list[str] = []
    response_line: str | None = None

    def drain_stderr():
        try:
            for line in p.stderr:
                stderr_buf.append(line)
                if len(stderr_buf) <= 80:
                    sys.stderr.write(f"[stderr] {line}")
        except Exception:
            pass

    th = threading.Thread(target=drain_stderr, daemon=True)
    th.start()

    # The orchestrator (rich.Console) prints progress to stdout by default,
    # so we must filter out non-JSON lines until we find the response with id=100.
    deadline = t0 + TIMEOUT_TOTAL
    while time.time() < deadline:
        rlist, _, _ = select.select([p.stdout], [], [], 5.0)
        if rlist:
            line = p.stdout.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            # Try to parse as JSON. If success and id matches, this is our response.
            if stripped.startswith("{"):
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict) and obj.get("id") == 100:
                        response_line = stripped
                        break
                except Exception:
                    pass
            # Otherwise it's a noise line (rich progress / log) — show & continue.
            sys.stdout.write(f"[stdout-noise] {stripped[:200]}\n")
            sys.stdout.flush()
            continue
        elapsed = time.time() - t0
        print(f"[wait] {elapsed:.0f}s elapsed, still waiting for tools/call response...")

    elapsed = time.time() - t0
    print(f"[run] response received after {elapsed:.1f}s")

    if not response_line:
        print("[run] TIMEOUT — no response within budget; killing server")
        p.kill()
        try:
            p.wait(timeout=3)
        except Exception:
            pass
        return 2

    # parse the response
    try:
        resp = json.loads(response_line)
    except Exception as e:
        print(f"[run] JSON parse failed: {e}; raw={response_line[:300]!r}")
        p.kill()
        return 3

    if "error" in resp:
        err = resp["error"]
        print(f"[run] ERROR code={err.get('code')}, msg={err.get('message')}")
    else:
        content = resp.get("result", {}).get("content", [])
        if content:
            text = content[0].get("text", "")
            print(f"[run] SUCCESS — first 600 chars of result text:")
            print(text[:600])
            try:
                obj = json.loads(text)
                tid = obj.get("task_id")
                fs = obj.get("final_status") or obj.get("status")
                cycles = obj.get("cycles_run") or obj.get("cycles")
                print(f"\n[run] parsed -> task_id={tid}, final_status={fs}, cycles={cycles}")
            except Exception as e:
                print(f"[run] result text not JSON: {e}")

    # close server cleanly
    p.stdin.close()
    try:
        rc = p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = -1
    print(f"[end] server exit rc={rc}")
    print(f"[end] stderr lines captured: {len(stderr_buf)}")
    if stderr_buf:
        tail = "".join(stderr_buf[-20:])
        print(f"[end] stderr tail:\n{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
