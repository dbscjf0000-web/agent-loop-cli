"""Tests for the Claude Code CLI provider in agent_loop.models.

All claude CLI invocations are mocked: subprocess.run is monkeypatched and
shutil.which is forced to return a fake path, so these tests run in any
environment.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config, Models
from agent_loop.models import (
    ModelResponse,
    _call_claude_cli,
    _claude_model_arg,
    _is_claude_model,
    call_model,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _CompletedProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_claude(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: _CompletedProc | None = None,
    raise_timeout: bool = False,
    captured: dict[str, Any] | None = None,
) -> None:
    """Force shutil.which to find claude and stub subprocess.run.

    Only resolves `claude` — anything else returns None so cursor / gemini /
    other binaries appear missing during the test.
    """

    def fake_which(name: str) -> str | None:
        return "/fake/claude" if name == "claude" else None

    monkeypatch.setattr(models_mod.shutil, "which", fake_which)

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if captured is not None:
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
        return proc or _CompletedProc(0, "default-stdout", "")

    monkeypatch.setattr(models_mod.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_is_claude_model_predicate() -> None:
    assert _is_claude_model("claude")
    assert _is_claude_model("claude/default")
    assert _is_claude_model("claude/opus")
    assert not _is_claude_model("anthropic/claude-opus-4-7")
    assert not _is_claude_model("cursor/auto")
    assert not _is_claude_model("gemini/gemini-2.5-pro")
    assert not _is_claude_model("")


def test_claude_model_arg_translation() -> None:
    assert _claude_model_arg("claude") == "default"
    assert _claude_model_arg("claude/default") == "default"
    assert _claude_model_arg("claude/opus") == "opus"


# ---------------------------------------------------------------------------
# _call_claude_cli
# ---------------------------------------------------------------------------

def test_call_claude_cli_success_returns_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_claude(monkeypatch, proc=_CompletedProc(0, "hello world\n", ""), captured=captured)

    resp = _call_claude_cli(
        prompt="say hi",
        system="be brief",
        model="default",
        workspace="/tmp/ws",
        timeout=42,
    )

    assert isinstance(resp, ModelResponse)
    assert resp.text == "hello world"
    assert resp.cost_usd == 0.0
    assert resp.model == "claude/default"
    assert resp.prompt_tokens >= 1
    assert resp.completion_tokens >= 1
    assert resp.latency_s >= 0.0

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/claude"
    assert "--print" in cmd
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "text"
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp/ws"
    # Last arg is the rendered prompt with system header.
    assert cmd[-1].startswith("# System")
    assert "be brief" in cmd[-1]
    assert "say hi" in cmd[-1]
    assert captured["kwargs"]["timeout"] == 42


def test_call_claude_cli_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_claude(monkeypatch, proc=_CompletedProc(1, "", "auth fail: please run `claude` to log in"))

    with pytest.raises(RuntimeError, match="claude CLI failed"):
        _call_claude_cli(prompt="x", model="default")


def test_call_claude_cli_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_claude(monkeypatch, raise_timeout=True)

    with pytest.raises(RuntimeError, match="timed out"):
        _call_claude_cli(prompt="x", model="default", timeout=1.0)


def test_call_claude_cli_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_mod.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not found on PATH"):
        _call_claude_cli(prompt="x", model="default")


# ---------------------------------------------------------------------------
# call_model dispatch
# ---------------------------------------------------------------------------

def test_call_model_routes_claude_through_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_claude(monkeypatch, proc=_CompletedProc(0, "from claude", ""), captured=captured)

    # If litellm is invoked, blow up — we want to prove dispatch went the claude route.
    def boom(**_: Any) -> None:
        raise AssertionError("litellm.completion must not be called for claude models")

    monkeypatch.setattr(models_mod.litellm, "completion", boom)

    cfg = Config(models=Models(
        research="anthropic/claude-opus-4-7",
        plan="anthropic/claude-opus-4-7",
        implement="anthropic/claude-sonnet-4-6",
        verify="claude/default",
        judge="openai/gpt-5.2",
    ))

    resp = call_model("verify", "ping", config=cfg, workspace="/tmp/agent-ws")
    assert resp.text == "from claude"
    assert resp.model == "claude/default"

    cmd = captured["cmd"]
    assert "/tmp/agent-ws" in cmd  # workspace flag was forwarded
    assert "--add-dir" in cmd
