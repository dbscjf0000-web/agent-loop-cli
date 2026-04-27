"""Live verification of `python -m agent_loop.cli mcp serve` via real subprocess.

Not a unit test (kept out of the pytest collection by convention — it spawns
a real server and exchanges JSON-RPC frames over stdin/stdout). Run with:

    LD_LIBRARY_PATH=/apps/applications/PYTHON/3.14.2/lib \\
        /apps/applications/PYTHON/3.14.2/bin/python3 tests/e2e_mcp_subprocess.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _start_server(extra_env: dict[str, str] | None = None,
                  config_path: str | None = None,
                  root: str | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "agent_loop.cli", "mcp", "serve"]
    if config_path:
        cmd += ["--config", config_path]
    if root:
        cmd += ["--root", root]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


def _send(p: subprocess.Popen, method: str, params=None, id=None, timeout: float = 10.0):
    req = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        req["params"] = params
    p.stdin.write(json.dumps(req) + "\n")
    p.stdin.flush()
    if id is None:  # notification — no response expected
        return None
    line = p.stdout.readline()
    if not line:
        return {"_raw": "", "_eof": True}
    return json.loads(line)


def _short(obj, n=200):
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return (s[:n] + "...") if len(s) > n else s


def main() -> int:
    print("=" * 78)
    print("LIVE subprocess e2e for `mcp serve` (v0.5.0)")
    print("=" * 78)
    results: list[tuple[str, str, str]] = []

    # --- Phase 1: minimal probes ----------------------------------------
    p = _start_server()
    print(f"[start] pid={p.pid}, cmd=python -m agent_loop.cli mcp serve")

    # 1) initialize
    r = _send(p, "initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {}, "clientInfo": {"name": "verify"}}, id=1)
    server_info = (r or {}).get("result", {}).get("serverInfo", {})
    print(f"[1] initialize -> serverInfo={server_info}")
    results.append(("initialize", "OK" if server_info.get("name") else "FAIL", _short(server_info)))

    # 2) tools/list
    r = _send(p, "tools/list", id=2)
    tools = [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]
    print(f"[2] tools/list -> {tools}")
    results.append(("tools/list", "OK" if len(tools) == 6 else f"FAIL ({len(tools)} tools)", _short(tools)))

    # 3) tools/call agent_loop.list
    r = _send(p, "tools/call", {"name": "agent_loop.list", "arguments": {}}, id=3)
    content = (r or {}).get("result", {}).get("content", [])
    raw = content[0].get("text", "") if content else ""
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        pass
    print(f"[3] tools/call agent_loop.list -> root={parsed.get('root') if parsed else 'N/A'}, "
          f"tasks={len(parsed.get('tasks', []) if parsed else [])}")
    results.append(("tools/call list",
                    "OK" if parsed and "tasks" in parsed else "FAIL",
                    _short(parsed if parsed else raw)))

    # 4) tools/call agent_loop.memory_show
    r = _send(p, "tools/call",
              {"name": "agent_loop.memory_show", "arguments": {"limit": 3}}, id=4)
    content = (r or {}).get("result", {}).get("content", [])
    raw = content[0].get("text", "") if content else ""
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        pass
    err = (r or {}).get("error", {})
    print(f"[4] tools/call memory_show -> "
          f"path={(parsed or {}).get('path') if parsed else 'N/A'}, "
          f"err={err.get('code') if err else None}")
    results.append(("tools/call memory_show",
                    "OK" if parsed or err else "FAIL",
                    _short(parsed if parsed else err)))

    # 5) resources/list
    r = _send(p, "resources/list", id=5)
    resources = [x["uri"] for x in (r or {}).get("result", {}).get("resources", [])]
    print(f"[5] resources/list -> {resources}")
    results.append(("resources/list",
                    "OK" if len(resources) == 4 else f"FAIL ({len(resources)})",
                    _short(resources)))

    # 6) resources/read missing task -> ERR_NOT_FOUND (-32001)
    r = _send(p, "resources/read",
              {"uri": "agent-loop://task/missing/solution"}, id=6)
    err = (r or {}).get("error") or {}
    print(f"[6] resources/read missing -> code={err.get('code')}, msg={err.get('message')}")
    results.append(("resources/read missing",
                    "OK" if err.get("code") == -32001 else "FAIL",
                    f"code={err.get('code')}"))

    # 7) unknown method -> -32601
    r = _send(p, "foo/bar", id=7)
    err = (r or {}).get("error") or {}
    print(f"[7] foo/bar -> code={err.get('code')}, msg={err.get('message')}")
    results.append(("unknown method",
                    "OK" if err.get("code") == -32601 else "FAIL",
                    f"code={err.get('code')}"))

    # 8) invalid params: tools/call missing 'name'
    r = _send(p, "tools/call", {"arguments": {}}, id=8)
    err = (r or {}).get("error") or {}
    print(f"[8] tools/call missing name -> code={err.get('code')}, msg={err.get('message')}")
    results.append(("invalid params",
                    "OK" if err.get("code") in (-32602,) else "FAIL",
                    f"code={err.get('code')}"))

    # close server
    p.stdin.close()
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = -1
    stderr_tail = (p.stderr.read() or "")[-400:]
    print(f"[end] server exit rc={rc}, stderr_tail={stderr_tail!r}")
    results.append(("server exit", "OK" if rc == 0 else f"FAIL ({rc})",
                    f"rc={rc}, stderr_len={len(stderr_tail)}"))

    # --- Summary --------------------------------------------------------
    print("\n" + "=" * 78)
    print("SUBPROCESS E2E SUMMARY")
    print("=" * 78)
    width = max(len(r[0]) for r in results) + 2
    for name, status, detail in results:
        print(f"  {name.ljust(width)} {status.ljust(10)} {detail}")
    fails = [r for r in results if not r[1].startswith("OK")]
    print(f"\nTotal: {len(results)} probes, {len(fails)} fail(s)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
