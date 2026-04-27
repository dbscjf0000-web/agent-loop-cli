"""Tests for the Gemini CLI provider in agent_loop.models.

All gemini CLI invocations are mocked: subprocess.run is monkeypatched and
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
    _call_gemini_cli,
    _gemini_model_arg,
    _is_gemini_model,
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


def _patch_gemini(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: _CompletedProc | None = None,
    raise_timeout: bool = False,
    captured: dict[str, Any] | None = None,
) -> None:
    """Force shutil.which to find gemini and stub subprocess.run.

    Only resolves `gemini` — anything else returns None.
    """

    def fake_which(name: str) -> str | None:
        return "/fake/gemini" if name == "gemini" else None

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

def test_is_gemini_model_predicate() -> None:
    assert _is_gemini_model("gemini")
    assert _is_gemini_model("gemini/gemini-2.5-pro")
    assert _is_gemini_model("gemini/gemini-2.5-flash")
    assert not _is_gemini_model("openai/gpt-5.2")
    assert not _is_gemini_model("cursor/auto")
    assert not _is_gemini_model("claude/default")
    assert not _is_gemini_model("")


def test_gemini_model_arg_translation() -> None:
    assert _gemini_model_arg("gemini") == "gemini-2.5-pro"
    assert _gemini_model_arg("gemini/gemini-2.5-pro") == "gemini-2.5-pro"
    assert _gemini_model_arg("gemini/gemini-2.5-flash") == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# _call_gemini_cli
# ---------------------------------------------------------------------------

def test_call_gemini_cli_success_returns_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_gemini(monkeypatch, proc=_CompletedProc(0, "hello world\n", ""), captured=captured)

    resp = _call_gemini_cli(
        prompt="say hi",
        system="be brief",
        model="gemini-2.5-pro",
        workspace="/tmp/ws",
        timeout=42,
    )

    assert isinstance(resp, ModelResponse)
    assert resp.text == "hello world"
    assert resp.cost_usd == 0.0
    assert resp.model == "gemini/gemini-2.5-pro"
    assert resp.prompt_tokens >= 1
    assert resp.completion_tokens >= 1
    assert resp.latency_s >= 0.0

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/gemini"
    assert "-p" in cmd
    # Prompt is the value right after -p.
    rendered = cmd[cmd.index("-p") + 1]
    assert rendered.startswith("# System")
    assert "be brief" in rendered
    assert "say hi" in rendered
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gemini-2.5-pro"
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "text"
    assert "--yolo" in cmd
    assert "--skip-trust" in cmd
    assert cmd[cmd.index("--include-directories") + 1] == "/tmp/ws"
    assert captured["kwargs"]["timeout"] == 42


def test_call_gemini_cli_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gemini(monkeypatch, proc=_CompletedProc(1, "", "auth fail: please run gemini login"))

    with pytest.raises(RuntimeError, match="gemini CLI failed"):
        _call_gemini_cli(prompt="x", model="gemini-2.5-pro")


def test_call_gemini_cli_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gemini(monkeypatch, raise_timeout=True)

    with pytest.raises(RuntimeError, match="timed out"):
        _call_gemini_cli(prompt="x", model="gemini-2.5-pro", timeout=1.0)


def test_call_gemini_cli_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_mod.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not found on PATH"):
        _call_gemini_cli(prompt="x", model="gemini-2.5-pro")


# ---------------------------------------------------------------------------
# call_model dispatch
# ---------------------------------------------------------------------------

def test_call_model_routes_gemini_through_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_gemini(monkeypatch, proc=_CompletedProc(0, "from gemini", ""), captured=captured)

    # If litellm is invoked, blow up — we want to prove dispatch went the gemini route.
    def boom(**_: Any) -> None:
        raise AssertionError("litellm.completion must not be called for gemini models")

    monkeypatch.setattr(models_mod.litellm, "completion", boom)

    cfg = Config(models=Models(
        research="anthropic/claude-opus-4-7",
        plan="anthropic/claude-opus-4-7",
        implement="anthropic/claude-sonnet-4-6",
        verify="anthropic/claude-haiku-4-5",
        judge="gemini/gemini-2.5-pro",
    ))

    resp = call_model("judge", "ping", config=cfg, workspace="/tmp/agent-ws")
    assert resp.text == "from gemini"
    assert resp.model == "gemini/gemini-2.5-pro"

    cmd = captured["cmd"]
    assert "/tmp/agent-ws" in cmd  # workspace flag was forwarded
    assert "--include-directories" in cmd
