"""v0.5.1: Orchestrator must respect injected ``console`` parameter.

Bug fixed in v0.5.1: when ``Orchestrator`` is constructed without a
``console=`` argument, every progress line lands on stdout via the rich
default ``Console()``. That breaks the MCP stdio transport because
JSON-RPC frames and progress chatter get interleaved on the same stream.

These tests pin the contract:

  1. ``Orchestrator(td, cfg)`` (no kwarg) defaults to a real
     ``rich.console.Console`` writing to stdout (CLI users see progress).
  2. ``Orchestrator(td, cfg, console=mock)`` actually routes every print
     through the injected console (MCP can swap in a stderr console).
  3. ``Console(file=sys.stderr)`` is honoured — all progress lands on
     stderr and stdout stays empty.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from agent_loop import models as models_mod
from agent_loop.config import Config
from agent_loop.orchestrator import Orchestrator
from agent_loop.state import TaskDir


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
    )


def _patch_completions(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """Wire the canned R/P/I/V/J fake responses used by these tests."""
    canned = {
        "research": "# Findings\n- ok",
        "plan": "# Plan\n1. write add()",
        "implement": (
            "## notes\nadd\n\n"
            "```python\n"
            "def add(a, b):\n    return a + b\n"
            "```\n"
        ),
        "verify": json.dumps({
            "axes": {"correctness": 1.0, "performance": 1.0},
            "weighted_score": 0.97,
            "evidence": "ok",
            "issues": [],
        }),
        "judge": json.dumps({"better": True, "action": "stop", "scores": {}}),
    }
    phase_by_model = {
        cfg.models.research: "research",
        cfg.models.plan: "plan",
        cfg.models.implement: "implement",
        cfg.models.verify: "verify",
        cfg.models.judge: "judge",
    }

    def fake_completion(**kw: Any) -> SimpleNamespace:
        return _resp(canned[phase_by_model[kw["model"]]])

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)


def test_default_console_writes_to_stdout(tmp_path: Path) -> None:
    """No ``console=`` kwarg -> rich.Console default (stdout)."""
    cfg = Config()
    td = TaskDir(root=tmp_path, task_id="orchCons1")
    orch = Orchestrator(td, cfg)
    assert isinstance(orch.console, Console)
    # rich's default Console writes to sys.stdout.
    assert orch.console.file is sys.stdout


def test_injected_console_receives_every_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mock console must capture each ``self.console.print`` from the run."""
    cfg = Config()
    _patch_completions(monkeypatch, cfg)

    mock_console = MagicMock(spec=Console)
    td = TaskDir(root=tmp_path, task_id="orchCons2")
    orch = Orchestrator(td, cfg, console=mock_console)
    result = orch.run("Implement add(a,b).", max_cycles=1, mode="auto", max_redo=1)

    assert result["final_status"] == "stop"
    # The orchestrator emits at least: cycle banner, 5 phase lines, judge line.
    # We don't pin the exact count (it can shift with future edits) — only
    # assert the routing happened at all.
    assert mock_console.print.call_count >= 3, (
        f"expected progress to flow through injected console, got "
        f"{mock_console.print.call_count} calls"
    )


def test_stderr_console_keeps_stdout_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v0.5.1 MCP fix: a stderr Console must keep stdout pristine."""
    cfg = Config()
    _patch_completions(monkeypatch, cfg)

    # Build the same kind of console MCP injects.
    err_buf = io.StringIO()
    out_buf = io.StringIO()
    err_console = Console(file=err_buf, force_terminal=False, width=120)

    td = TaskDir(root=tmp_path, task_id="orchCons3")
    orch = Orchestrator(td, cfg, console=err_console)
    # Redirect real stdout while running so any *bare* print() leak (none
    # expected) would land in out_buf.
    real_out = sys.stdout
    sys.stdout = out_buf
    try:
        orch.run("Implement add(a,b).", max_cycles=1, mode="auto", max_redo=1)
    finally:
        sys.stdout = real_out

    err_text = err_buf.getvalue()
    out_text = out_buf.getvalue()
    # Progress must show up on stderr.
    assert "Cycle 1" in err_text, f"expected cycle banner on stderr, got: {err_text!r}"
    # Stdout must be empty (the orchestrator's only output channel is the
    # injected console).
    assert out_text == "", f"stdout should be empty, got: {out_text!r}"
