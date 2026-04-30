"""Tests for the codex CLI provider (added in v0.8 — codex provider).

Mirrors the cursor / claude / gemini test conventions: monkeypatch
``shutil.which`` so the provider check passes, monkeypatch
``subprocess.run`` so we never spawn the real codex CLI, and verify the
command line + return-value mapping.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from agent_loop import models
from agent_loop.config import Config


def test_is_codex_model_recognises_prefix_and_bare() -> None:
    assert models._is_codex_model("codex") is True
    assert models._is_codex_model("codex/gpt-5.2-codex-high") is True
    assert models._is_codex_model("codex/anything") is True
    assert models._is_codex_model("cursor/composer-2") is False
    assert models._is_codex_model("openai/gpt-4") is False
    assert models._is_codex_model("") is False


def test_codex_model_arg_extraction() -> None:
    assert models._codex_model_arg("codex") is None  # bare -> use codex default
    assert models._codex_model_arg("codex/gpt-5.2-codex-high") == "gpt-5.2-codex-high"
    assert models._codex_model_arg("codex/o3") == "o3"


def test_cli_provider_dispatches_codex() -> None:
    assert models._cli_provider("codex") == "codex"
    assert models._cli_provider("codex/gpt-5.4") == "codex"
    assert models._cli_provider("cursor/composer-2") == "cursor"
    assert models._cli_provider("anthropic/claude-3-5-sonnet") is None


def test_call_codex_cli_invokes_subprocess_with_bypass_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass flag is mandatory — without it, the I phase cannot write
    workspace/solution.py because codex's default sandbox is read-only.
    """
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="OK", stderr=""
        )

    monkeypatch.setattr(models.shutil, "which", lambda _: "/fake/codex")
    monkeypatch.setattr(models.subprocess, "run", fake_run)

    resp = models._call_codex_cli(
        "hello",
        system="be brief",
        model="gpt-5.2-codex-high",
        workspace="/tmp/some_ws",
        timeout=30.0,
    )

    cmd = captured["cmd"]
    # Mandatory flags
    assert cmd[0] == "/fake/codex"
    assert cmd[1] == "exec"
    assert "--skip-git-repo-check" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    # Stdin sentinel
    assert "-" in cmd
    # Per-phase model override propagated as -m
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.2-codex-high"
    # workspace becomes cwd, prompt+system go on stdin
    assert captured["cwd"] == "/tmp/some_ws"
    assert "hello" in captured["input"]
    assert "be brief" in captured["input"]
    # Return value mapping
    assert resp.text == "OK"
    assert resp.model == "codex/gpt-5.2-codex-high"
    assert resp.cost_usd == 0.0


def test_call_codex_cli_uses_default_model_when_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``codex`` (no slash) means: don't pass -m, let codex use the model
    set in ~/.codex/config.toml."""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(models.shutil, "which", lambda _: "/fake/codex")
    monkeypatch.setattr(models.subprocess, "run", fake_run)

    resp = models._call_codex_cli("hi", model=None, timeout=10.0)
    assert "-m" not in captured["cmd"]
    assert resp.model == "codex"


def test_call_codex_cli_raises_on_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="codex CLI not found"):
        models._call_codex_cli("hi", timeout=5.0)


def test_call_codex_cli_raises_on_nonzero_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=cmd, returncode=2, stdout="", stderr="auth error"
        )

    monkeypatch.setattr(models.shutil, "which", lambda _: "/fake/codex")
    monkeypatch.setattr(models.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="codex CLI failed.*auth error"):
        models._call_codex_cli("hi", timeout=5.0)


def test_call_codex_cli_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd, 1.0)

    monkeypatch.setattr(models.shutil, "which", lambda _: "/fake/codex")
    monkeypatch.setattr(models.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="codex CLI timed out"):
        models._call_codex_cli("hi", timeout=1.0)


def test_call_model_routes_codex_through_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Integration: configure a phase with codex/<model> and verify
    call_model dispatches into _call_codex_cli (not litellm)."""
    cfg = Config()
    cfg.models.research = "codex/gpt-5.2-codex-high"

    captured: dict[str, Any] = {}

    def fake_codex(*args: Any, **kwargs: Any) -> models.ModelResponse:
        captured["called"] = True
        captured["model"] = kwargs.get("model")
        return models.ModelResponse(
            text="ok", prompt_tokens=1, completion_tokens=1,
            cost_usd=0.0, latency_s=0.01, model="codex/gpt-5.2-codex-high",
        )

    monkeypatch.setattr(models, "_call_codex_cli", fake_codex)
    # Make sure litellm is NOT called.
    monkeypatch.setattr(
        models, "_call_litellm",
        lambda *a, **k: pytest.fail("litellm should not be called for codex/* models"),
    )

    resp = models.call_model("research", "hi", system="", config=cfg)
    assert captured.get("called") is True
    assert captured.get("model") == "gpt-5.2-codex-high"
    assert resp.text == "ok"
