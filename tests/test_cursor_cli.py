"""Tests for the cursor-agent CLI provider in agent_loop.models.

All cursor-agent invocations are mocked: subprocess.run is monkeypatched and
shutil.which is forced to return a fake path, so these tests run in any
environment.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config, Models
from agent_loop.models import (
    ModelResponse,
    _call_cursor_cli,
    _cursor_model_arg,
    _is_cursor_model,
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


def _patch_cursor(monkeypatch: pytest.MonkeyPatch, *, proc: _CompletedProc | None = None,
                  raise_timeout: bool = False, captured: dict[str, Any] | None = None) -> None:
    """Force shutil.which to find cursor-agent and stub subprocess.run."""
    monkeypatch.setattr(models_mod.shutil, "which", lambda name: "/fake/cursor-agent")

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

def test_is_cursor_model_true_for_cursor_and_prefixed() -> None:
    assert _is_cursor_model("cursor")
    assert _is_cursor_model("cursor/auto")
    assert _is_cursor_model("cursor/sonnet-4")
    assert not _is_cursor_model("anthropic/claude-opus-4-7")
    assert not _is_cursor_model("")
    assert not _is_cursor_model("openai/gpt-5.2")


def test_cursor_model_arg_translation() -> None:
    assert _cursor_model_arg("cursor") == "auto"
    assert _cursor_model_arg("cursor/auto") == "auto"
    assert _cursor_model_arg("cursor/sonnet-4") == "sonnet-4"
    assert _cursor_model_arg("cursor/gpt-5") == "gpt-5"


# ---------------------------------------------------------------------------
# _call_cursor_cli
# ---------------------------------------------------------------------------

def test_call_cursor_cli_success_returns_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_cursor(monkeypatch, proc=_CompletedProc(0, "hello world\n", ""), captured=captured)

    resp = _call_cursor_cli(
        prompt="say hi",
        system="be brief",
        model="auto",
        workspace="/tmp/ws",
        timeout=42,
    )

    assert isinstance(resp, ModelResponse)
    assert resp.text == "hello world"
    assert resp.cost_usd == 0.0
    assert resp.model == "cursor/auto"
    assert resp.prompt_tokens >= 1
    assert resp.completion_tokens >= 1
    assert resp.latency_s >= 0.0

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/cursor-agent"
    assert "--print" in cmd
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "text"
    assert "--force" in cmd
    assert "--trust" in cmd
    assert cmd[cmd.index("--model") + 1] == "auto"
    assert cmd[cmd.index("--workspace") + 1] == "/tmp/ws"
    # Last arg is the rendered prompt with system header.
    assert cmd[-1].startswith("# System")
    assert "be brief" in cmd[-1]
    assert "say hi" in cmd[-1]
    assert captured["kwargs"]["timeout"] == 42


def test_call_cursor_cli_no_system_skips_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_cursor(monkeypatch, proc=_CompletedProc(0, "ok", ""), captured=captured)

    _call_cursor_cli(prompt="bare prompt", system="", model="auto")
    rendered = captured["cmd"][-1]
    assert rendered == "bare prompt"


def test_call_cursor_cli_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cursor(monkeypatch, proc=_CompletedProc(1, "", "auth fail: please run `cursor-agent login`"))

    with pytest.raises(RuntimeError, match="cursor-agent failed"):
        _call_cursor_cli(prompt="x", model="auto")


def test_call_cursor_cli_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cursor(monkeypatch, raise_timeout=True)

    with pytest.raises(RuntimeError, match="timed out"):
        _call_cursor_cli(prompt="x", model="auto", timeout=1.0)


def test_call_cursor_cli_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_mod.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not found on PATH"):
        _call_cursor_cli(prompt="x", model="auto")


# ---------------------------------------------------------------------------
# call_model dispatch
# ---------------------------------------------------------------------------

def test_call_model_routes_cursor_through_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_cursor(monkeypatch, proc=_CompletedProc(0, "from cursor", ""), captured=captured)

    # If litellm is invoked, blow up — we want to prove dispatch went the cursor route.
    def boom(**_: Any) -> None:
        raise AssertionError("litellm.completion must not be called for cursor models")

    monkeypatch.setattr(models_mod.litellm, "completion", boom)

    cfg = Config(models=Models(
        research="cursor/auto",
        plan="anthropic/claude-opus-4-7",
        implement="anthropic/claude-sonnet-4-6",
        verify="anthropic/claude-haiku-4-5",
        judge="openai/gpt-5.2",
    ))

    resp = call_model("research", "ping", config=cfg, workspace="/tmp/agent-ws")
    assert resp.text == "from cursor"
    assert resp.model == "cursor/auto"

    cmd = captured["cmd"]
    assert "/tmp/agent-ws" in cmd  # workspace flag was forwarded


def test_call_model_routes_non_cursor_through_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-cursor model must NOT shell out to cursor-agent."""
    monkeypatch.setattr(models_mod.shutil, "which", lambda name: "/fake/cursor-agent")

    def fail_subprocess(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("subprocess.run must not be called for litellm models")

    monkeypatch.setattr(models_mod.subprocess, "run", fail_subprocess)

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="from litellm"))],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
    )
    monkeypatch.setattr(models_mod.litellm, "completion", lambda **_: fake_resp)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.001)

    cfg = Config()  # default: anthropic/* + openai/*
    resp = call_model("research", "ping", config=cfg)
    assert resp.text == "from litellm"
    assert resp.model == cfg.models.research
