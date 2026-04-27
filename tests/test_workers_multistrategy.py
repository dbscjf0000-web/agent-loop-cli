"""Tests for workers.run_plan multi-strategy dispatch (v0.3).

Confirms:
  - single mode preserved when runtime.strategies is None (back-compat).
  - multi mode writes proposals.json + plan_selector.json + plan.md (winner).
  - winner.text is byte-identical to plan.md.
  - run_plan signature unchanged (TaskDir, Config) -> ModelResponse.
  - config schema accepts list[str] / list[dict] / None.
  - ENV var override.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop import strategy_engine as se_mod
from agent_loop import workers
from agent_loop.config import Config, Runtime, StrategySpec, load_config
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir


_GOOD_PLAN = (
    "# Plan A\n"
    "## Approach\n"
    "1. step one\n"
    "2. step two\n"
    "```python\nfn()\n```\n"
    + "filler text " * 60
)


def _resp(text: str, model: str = "mock") -> ModelResponse:
    return ModelResponse(
        text=text, prompt_tokens=10, completion_tokens=20,
        cost_usd=0.0, latency_s=0.05, model=model,
    )


# ---------------------------------------------------------------------------
# 1. single mode preserved when runtime.strategies is None
# ---------------------------------------------------------------------------

def test_run_plan_single_mode_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="single-plan")
    td.init()
    td.task_md_path().write_text("write hello world", encoding="utf-8")

    def fake_completion(**kw):
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="single plan output"))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
        )

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    cfg = Config()  # runtime.strategies = None
    resp = workers.run_plan(td, cfg)
    assert isinstance(resp, ModelResponse)
    assert td.read_artifact("plan.md") == "single plan output"
    # single mode -> no multi-strategy artifacts
    assert not td.has_artifact("proposals.json")
    assert not td.has_artifact("plan_selector.json")


# ---------------------------------------------------------------------------
# 2. multi mode writes proposals.json + plan_selector.json + plan.md
# ---------------------------------------------------------------------------

def test_run_plan_multi_writes_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="multi-plan")
    td.init()
    td.task_md_path().write_text("write hello world", encoding="utf-8")

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        if "Below are candidate PLANS" in prompt:
            # selector rubric — pick claude
            return _resp(json.dumps({
                "winner_index": 0, "reason": "claude wins",
                "scores": [0.95, 0.55, 0.30],
            }), model="selector")
        prov = config.models.plan if config else "?"
        return _resp(_GOOD_PLAN + f"\n[from {prov}]", model=prov)

    monkeypatch.setattr(se_mod, "call_model", fake_call_model)

    cfg = Config(runtime=Runtime(strategies=[
        StrategySpec(provider="claude/default"),
        StrategySpec(provider="gemini/gemini-2.5-flash"),
        StrategySpec(provider="cursor/auto"),
    ]))
    resp = workers.run_plan(td, cfg)

    assert isinstance(resp, ModelResponse)
    # All three artifacts present.
    assert td.has_artifact("plan.md")
    assert td.has_artifact("proposals.json")
    assert td.has_artifact("plan_selector.json")

    # plan.md is the winner's full text (downstream reads this).
    plan_md = td.read_artifact("plan.md")
    assert "[from claude/default]" in plan_md  # winner = index 0

    # proposals.json contains all three with no errors
    proposals = td.read_artifact("proposals.json")
    assert isinstance(proposals, dict)
    assert len(proposals["proposals"]) == 3
    assert all(p["error"] is None for p in proposals["proposals"])

    # selector audit
    sel = td.read_artifact("plan_selector.json")
    assert sel["winner_index"] == 0
    assert sel["winner_provider"] == "claude/default"
    assert sel["selector_method"] == "heuristic+llm"

    # ModelResponse model name surfaces strategy origin
    assert "strategy:" in resp.model
    assert "claude/default" in resp.model


# ---------------------------------------------------------------------------
# 3. winner.text == plan.md byte-identical
# ---------------------------------------------------------------------------

def test_winner_text_equals_plan_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="winner-eq")
    td.init()
    td.task_md_path().write_text("a task", encoding="utf-8")

    winner_text = "# very specific winner text\n## detail\n1. one\n```py\nx\n```"

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        if "Below are candidate PLANS" in prompt:
            return _resp(json.dumps({
                "winner_index": 0, "reason": "good", "scores": [0.9, 0.1],
            }), model="selector")
        prov = config.models.plan if config else "?"
        if prov == "winner":
            return _resp(winner_text, model=prov)
        return _resp("loser plan", model=prov)

    monkeypatch.setattr(se_mod, "call_model", fake_call_model)

    cfg = Config(runtime=Runtime(strategies=[
        StrategySpec(provider="winner"),
        StrategySpec(provider="loser"),
    ]))
    workers.run_plan(td, cfg)

    plan_md = td.read_artifact("plan.md")
    proposals = td.read_artifact("proposals.json")
    selector = td.read_artifact("plan_selector.json")
    winner_idx = selector["winner_index"]
    assert plan_md == winner_text
    assert proposals["proposals"][winner_idx]["text"] == plan_md


# ---------------------------------------------------------------------------
# 4. config: list[str] form normalized to StrategySpec
# ---------------------------------------------------------------------------

def test_config_strategies_normalize_str_list() -> None:
    cfg = Config.model_validate({
        "runtime": {"strategies": ["claude/default", "gemini/gemini-2.5-flash"]}
    })
    assert cfg.runtime.strategies is not None
    assert len(cfg.runtime.strategies) == 2
    assert cfg.runtime.strategies[0].provider == "claude/default"
    assert cfg.runtime.strategies[0].weight == 1.0


def test_config_strategies_empty_list_means_none() -> None:
    cfg = Config.model_validate({"runtime": {"strategies": []}})
    assert cfg.runtime.strategies is None


def test_config_strategies_negative_weight_rejected() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        Config.model_validate({
            "runtime": {"strategies": [{"provider": "x", "weight": 0}]}
        })


# ---------------------------------------------------------------------------
# 5. ENV var override
# ---------------------------------------------------------------------------

def test_env_strategies_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AGENT_LOOP_RUNTIME_STRATEGIES",
        "claude/default, gemini/gemini-2.5-flash , cursor/auto",
    )
    cfg = load_config()
    assert cfg.runtime.strategies is not None
    provs = [s.provider for s in cfg.runtime.strategies]
    assert provs == ["claude/default", "gemini/gemini-2.5-flash", "cursor/auto"]
    assert all(s.weight == 1.0 for s in cfg.runtime.strategies)


# ---------------------------------------------------------------------------
# 6. AllStrategiesFailed propagates from run_plan (no silent fallback)
# ---------------------------------------------------------------------------

def test_run_plan_all_fail_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="multi-allfail")
    td.init()
    td.task_md_path().write_text("a task", encoding="utf-8")

    def boom(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        raise RuntimeError("CLI down everywhere")

    monkeypatch.setattr(se_mod, "call_model", boom)

    cfg = Config(runtime=Runtime(strategies=[
        StrategySpec(provider="A"),
        StrategySpec(provider="B"),
    ]))
    from agent_loop.strategy_engine import AllStrategiesFailed
    with pytest.raises(AllStrategiesFailed):
        workers.run_plan(td, cfg)
