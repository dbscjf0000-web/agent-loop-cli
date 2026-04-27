"""Baseline runner: single ``cursor-agent`` call, no R/P/I/V/J cycle.

Used by ``benchmarks/compare.py`` to quantify what the 5-phase loop buys
over a one-shot call to the same underlying CLI provider.

Public surface:
    run_baseline(task_text, workspace, *, timeout=180) -> dict
        - executes ``cursor-agent --print --output-format text ... <prompt>``
        - extracts the first ```python``` block from stdout
        - writes ``<workspace>/solution.py``
        - returns ``{"latency_s", "code_chars", "stdout_chars",
                     "returncode", "extracted"}``

The function never raises on subprocess failure: a timeout / non-zero exit
is recorded in the returned dict so the caller (compare.py) can score the
empty-or-broken solution as 0 and keep the run in the statistics.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

__all__ = ["run_baseline", "extract_python"]


_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_python(text: str) -> str:
    """Extract the first fenced ```python block; otherwise return raw text.

    The baseline prompt asks for a single fenced block, so a clean response
    matches the regex on the first try. We fall back to the raw text only as
    a last-ditch attempt — most cursor responses include the fence.
    """
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip() + "\n"
    # last-ditch: maybe model returned raw code without fences
    stripped = text.strip()
    if not stripped:
        return ""
    # crude heuristic: only treat as raw code if it parses as Python-ish lines
    # (presence of `def `, `class `, `import ` somewhere). Otherwise return "".
    if any(kw in stripped for kw in ("def ", "class ", "import ", "from ")):
        return stripped + "\n"
    return ""


def run_baseline(
    task_text: str,
    workspace: Path,
    *,
    timeout: int = 180,
    model: str | None = None,
) -> dict[str, Any]:
    """Single-shot cursor-agent call. Writes ``workspace/solution.py``.

    Parameters
    ----------
    task_text:
        The task description (typically the YAML's ``task`` block, possibly
        annotated with success criteria — caller decides).
    workspace:
        Directory where ``solution.py`` will be written. Must exist.
    timeout:
        Seconds for the subprocess. On timeout we still return a dict with
        ``returncode=-1`` and empty solution.
    model:
        Optional cursor model id (e.g. ``"sonnet-4"``). When ``None`` we let
        cursor pick its default (``auto``). The agent-loop side uses the
        same default so the comparison is apples-to-apples.

    Returns
    -------
    dict with keys ``latency_s``, ``code_chars``, ``stdout_chars``,
    ``returncode``, ``extracted`` (bool — fence was found), ``timed_out``.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    prompt = (
        f"{task_text.strip()}\n\n"
        "Reply with Python code only in a ```python``` fenced block. "
        "No explanation, no surrounding prose. The code must be a complete "
        "module that defines the requested function(s) at top level."
    )

    cmd = [
        "cursor-agent",
        "--print",
        "--output-format",
        "text",
        "--force",
        "--trust",
    ]
    if model:
        cmd.extend(["--model", model])
    # Pass workspace via flag so cursor sees the directory (matches
    # how agent-loop's models.call_model invokes cursor-agent).
    cmd.extend([f"--workspace={workspace}", prompt])

    started = time.monotonic()
    timed_out = False
    rc = 0
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = -1
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except FileNotFoundError as exc:
        # cursor-agent not on PATH -> propagate as a recorded failure
        rc = -2
        stderr = f"FileNotFoundError: {exc}"

    latency = time.monotonic() - started

    code = extract_python(stdout)
    extracted = bool(code.strip())
    sol_path = workspace / "solution.py"
    sol_path.write_text(code, encoding="utf-8")

    # write a tiny audit log so failures are debuggable
    audit = workspace / "baseline_audit.txt"
    audit.write_text(
        f"cmd: {' '.join(cmd[:6])} ... <prompt {len(prompt)}ch>\n"
        f"rc={rc} timed_out={timed_out} latency_s={latency:.3f}\n"
        f"stdout_chars={len(stdout)} code_chars={len(code)} extracted={extracted}\n"
        f"--- stdout (first 800ch) ---\n{stdout[:800]}\n"
        f"--- stderr (first 400ch) ---\n{stderr[:400]}\n",
        encoding="utf-8",
    )

    return {
        "latency_s": round(latency, 3),
        "code_chars": len(code),
        "stdout_chars": len(stdout),
        "returncode": rc,
        "extracted": extracted,
        "timed_out": timed_out,
    }
