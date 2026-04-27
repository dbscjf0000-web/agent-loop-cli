"""Tests for v0.3.1 phase-aware ``cli_timeout`` plumbing.

Covers:
  - default: cli_timeout = 600 for every phase.
  - per-phase override: cli_timeout_verify > cli_timeout default.
  - env var override (default + per-phase).
  - ``Runtime.cli_timeout_for(phase)`` helper branching.
  - models.call_model honors the phase-aware timeout when routing to a CLI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config, Runtime, load_config


# ---------------------------------------------------------------------------
# 1. Default values
# ---------------------------------------------------------------------------

def test_runtime_cli_timeout_defaults() -> None:
    rt = Runtime()
    assert rt.cli_timeout == 600
    assert rt.cli_timeout_research is None
    assert rt.cli_timeout_plan is None
    assert rt.cli_timeout_implement is None
    assert rt.cli_timeout_verify is None
    assert rt.cli_timeout_judge is None
    # Helper resolves every phase to the default when no override is set
    for phase in ("research", "plan", "implement", "verify", "judge"):
        assert rt.cli_timeout_for(phase) == 600


# ---------------------------------------------------------------------------
# 2. Per-phase override wins over default
# ---------------------------------------------------------------------------

def test_runtime_cli_timeout_for_phase_override() -> None:
    rt = Runtime(cli_timeout=300, cli_timeout_verify=900, cli_timeout_judge=120)
    assert rt.cli_timeout_for("verify") == 900   # explicit override
    assert rt.cli_timeout_for("judge") == 120    # explicit override
    assert rt.cli_timeout_for("research") == 300  # falls back to default
    assert rt.cli_timeout_for("plan") == 300
    assert rt.cli_timeout_for("implement") == 300
    # Unknown phase -> default
    assert rt.cli_timeout_for("bogus") == 300


# ---------------------------------------------------------------------------
# 3. ENV override — default
# ---------------------------------------------------------------------------

def test_env_cli_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CLI_TIMEOUT", "180")
    cfg = load_config()
    assert cfg.runtime.cli_timeout == 180
    assert cfg.runtime.cli_timeout_for("verify") == 180
    assert cfg.runtime.cli_timeout_for("judge") == 180


# ---------------------------------------------------------------------------
# 4. ENV override — per-phase
# ---------------------------------------------------------------------------

def test_env_cli_timeout_per_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CLI_TIMEOUT", "200")
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_VERIFY", "900")
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_JUDGE", "60")
    cfg = load_config()
    assert cfg.runtime.cli_timeout == 200
    assert cfg.runtime.cli_timeout_verify == 900
    assert cfg.runtime.cli_timeout_judge == 60
    assert cfg.runtime.cli_timeout_for("verify") == 900
    assert cfg.runtime.cli_timeout_for("judge") == 60
    assert cfg.runtime.cli_timeout_for("plan") == 200


# ---------------------------------------------------------------------------
# 5. call_model honors phase-aware timeout when routing to a CLI
# ---------------------------------------------------------------------------

def test_call_model_passes_phase_timeout_to_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """call_model should pull cli_timeout from cfg.runtime.cli_timeout_for(phase)."""
    captured: dict[str, Any] = {}

    def fake_claude(prompt: str, system: str = "", **kw: Any):
        captured["timeout"] = kw.get("timeout")
        captured["model"] = kw.get("model")
        return models_mod.ModelResponse(
            text="ok",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            latency_s=0.01,
            model="claude/default",
        )

    monkeypatch.setattr(models_mod, "_call_claude_cli", fake_claude)

    cfg = Config()
    cfg.models.verify = "claude/default"
    cfg.runtime.cli_timeout = 300
    cfg.runtime.cli_timeout_verify = 1200

    models_mod.call_model("verify", "ping", config=cfg, workspace=tmp_path)
    assert captured["timeout"] == 1200.0


def test_call_model_falls_back_to_default_when_no_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When per-phase override is unset, the runtime-wide default applies."""
    captured: dict[str, Any] = {}

    def fake_gemini(prompt: str, system: str = "", **kw: Any):
        captured["timeout"] = kw.get("timeout")
        return models_mod.ModelResponse(
            text="ok",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            latency_s=0.01,
            model="gemini/gemini-2.5-flash",
        )

    monkeypatch.setattr(models_mod, "_call_gemini_cli", fake_gemini)

    cfg = Config()
    cfg.models.judge = "gemini/gemini-2.5-flash"
    cfg.runtime.cli_timeout = 250
    # cli_timeout_judge stays None -> default 250 wins

    models_mod.call_model("judge", "ping", config=cfg, workspace=tmp_path)
    assert captured["timeout"] == 250.0


def test_call_model_explicit_cli_timeout_kwarg_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``cli_timeout=...`` kwarg overrides whatever config says."""
    captured: dict[str, Any] = {}

    def fake_cursor(prompt: str, system: str = "", **kw: Any):
        captured["timeout"] = kw.get("timeout")
        return models_mod.ModelResponse(
            text="ok",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
            latency_s=0.01,
            model="cursor/auto",
        )

    monkeypatch.setattr(models_mod, "_call_cursor_cli", fake_cursor)

    cfg = Config()
    cfg.models.research = "cursor/auto"
    cfg.runtime.cli_timeout = 200
    cfg.runtime.cli_timeout_research = 999  # would normally win

    models_mod.call_model(
        "research", "ping", config=cfg, workspace=tmp_path, cli_timeout=42.0
    )
    # explicit kwarg trumps both per-phase and default config
    assert captured["timeout"] == 42.0


# ---------------------------------------------------------------------------
# 6. CLI flag plumbing — `--cli-timeout-verify` lands in cfg.runtime
# ---------------------------------------------------------------------------

def test_cli_override_helper_applies_v031_flags() -> None:
    """The internal `_override_runtime_v031` helper updates cfg.runtime in place."""
    from agent_loop.cli import _override_runtime_v031

    cfg = Config()
    out = _override_runtime_v031(
        cfg,
        cli_timeout=400,
        cli_timeout_verify=1200,
        cli_timeout_judge=100,
        judge_always_llm=False,
    )
    assert out.runtime.cli_timeout == 400
    assert out.runtime.cli_timeout_verify == 1200
    assert out.runtime.cli_timeout_judge == 100
    assert out.runtime.judge_always_llm is False
    assert out.runtime.cli_timeout_for("verify") == 1200
    assert out.runtime.cli_timeout_for("judge") == 100
    assert out.runtime.cli_timeout_for("plan") == 400


def test_cli_override_helper_no_change_when_all_none() -> None:
    """All-None args leave cfg.runtime untouched (back-compat default 600)."""
    from agent_loop.cli import _override_runtime_v031

    cfg = Config()
    out = _override_runtime_v031(
        cfg,
        cli_timeout=None,
        cli_timeout_verify=None,
        cli_timeout_judge=None,
        judge_always_llm=False,
    )
    assert out.runtime.cli_timeout == 600
    assert out.runtime.cli_timeout_verify is None
    assert out.runtime.cli_timeout_judge is None
    assert out.runtime.judge_always_llm is False
