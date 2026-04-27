"""Unit tests for v0.3 StrategyEngine — multi-strategy plan fan-out + Selector.

Every test mocks ``call_model`` so no real CLI / litellm is invoked.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_loop import strategy_engine as se_mod
from agent_loop.config import Config, StrategySpec
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir
from agent_loop.strategy_engine import (
    AllStrategiesFailed,
    PlanProposal,
    SelectionResult,
    StrategyEngine,
    _score_heuristic,
    proposal_to_dict,
    selection_to_dict,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_GOOD_PLAN_A = (
    "# Plan A\n"
    "## Approach\n"
    "1. Step one with detail\n"
    "2. Step two with detail\n"
    "3. Step three\n"
    "```python\n"
    "def f():\n"
    "    return 1\n"
    "```\n"
    "## Risks\n"
    "- handle edge cases\n"
    + "filler text " * 80
)

_OK_PLAN_B = (
    "# Plan B\n"
    "Just prose without much structure. "
    + "filler " * 200
)

_BAD_PLAN_C = "tiny plan."


def _resp(text: str, model: str = "mock") -> ModelResponse:
    return ModelResponse(
        text=text,
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.0,
        latency_s=0.1,
        model=model,
    )


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id="strategy-engine")
    td.init()
    return td


def _engine(tmp_path: Path) -> StrategyEngine:
    return StrategyEngine(_td(tmp_path), Config())


def _patch_call_model(
    monkeypatch: pytest.MonkeyPatch,
    by_provider: dict[str, Any],
    selector_response: Any = None,
) -> dict[str, Any]:
    """Patch strategy_engine.call_model. ``selector_response`` is used for
    the LLM rubric call (which calls ``cfg.models.plan`` unchanged after
    fan-out; we detect it by looking at the prompt content).
    """
    info: dict[str, Any] = {"providers": [], "selector_called": 0}

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        if "Below are candidate PLANS" in (prompt or ""):
            info["selector_called"] += 1
            if isinstance(selector_response, Exception):
                raise selector_response
            if isinstance(selector_response, ModelResponse):
                return selector_response
            if selector_response is None:
                # default: prefer index 0
                import json as _j
                return _resp(_j.dumps({
                    "winner_index": 0, "reason": "first looks best",
                    "scores": [0.9] + [0.5] * 10,
                }), model="selector")
            import json as _j
            return _resp(_j.dumps(selector_response), model="selector")

        prov = config.models.plan if config else "?"
        info["providers"].append(prov)
        spec = by_provider.get(prov)
        if spec is None:
            raise RuntimeError(f"no mock for provider={prov}")
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, ModelResponse):
            return spec
        return _resp(spec, model=prov)

    monkeypatch.setattr(se_mod, "call_model", fake_call_model)
    return info


# ---------------------------------------------------------------------------
# 1. happy path: 3 strategies, all OK -> selector picks heuristic+llm winner
# ---------------------------------------------------------------------------

def test_fanout_three_strategies_all_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    info = _patch_call_model(
        monkeypatch,
        {
            "claude/default": _GOOD_PLAN_A,
            "gemini/gemini-2.5-flash": _OK_PLAN_B,
            "cursor/auto": _BAD_PLAN_C,
        },
        selector_response={
            "winner_index": 0,
            "reason": "claude plan is most concrete",
            "scores": [0.95, 0.55, 0.20],
        },
    )
    strategies = [
        StrategySpec(provider="claude/default"),
        StrategySpec(provider="gemini/gemini-2.5-flash"),
        StrategySpec(provider="cursor/auto"),
    ]
    result = eng.fanout(strategies, prompt="plan me")
    assert isinstance(result, SelectionResult)
    assert result.winner_index == 0
    assert result.winner.provider == "claude/default"
    assert result.selector_method == "heuristic+llm"
    # 3 plan calls + 1 selector call
    assert sorted(info["providers"]) == [
        "claude/default", "cursor/auto", "gemini/gemini-2.5-flash",
    ]
    assert info["selector_called"] == 1
    # all proposals retained
    assert len(result.proposals) == 3
    assert all(p.error is None for p in result.proposals)
    # scores include both structural and llm columns
    assert result.scores[0]["llm"] == pytest.approx(0.95)
    assert "structural" in result.scores[0]


# ---------------------------------------------------------------------------
# 2. partial failure: one strategy fails -> selector runs on remaining
# ---------------------------------------------------------------------------

def test_partial_failure_one_strategy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    info = _patch_call_model(
        monkeypatch,
        {
            "A": _GOOD_PLAN_A,
            "B": RuntimeError("CLI down"),
            "C": _OK_PLAN_B,
        },
        selector_response={
            "winner_index": 0,
            "reason": "A wins",
            "scores": [0.9, 0.0, 0.6],
        },
    )
    strategies = [
        StrategySpec(provider="A"),
        StrategySpec(provider="B"),
        StrategySpec(provider="C"),
    ]
    result = eng.fanout(strategies, prompt="x")
    assert result.winner_index == 0
    errors = [p.error for p in result.proposals]
    assert errors[0] is None
    assert errors[1] is not None and "CLI down" in errors[1]
    assert errors[2] is None
    # selector still ran (>=2 valid proposals)
    assert info["selector_called"] == 1
    # failed proposal's score row marked with error and final=0
    assert result.scores[1]["error"] == errors[1]
    assert result.scores[1]["final"] == 0.0


# ---------------------------------------------------------------------------
# 3. all strategies fail -> AllStrategiesFailed
# ---------------------------------------------------------------------------

def test_all_strategies_fail_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": RuntimeError("a"),
        "B": RuntimeError("b"),
    })
    strategies = [StrategySpec(provider="A"), StrategySpec(provider="B")]
    with pytest.raises(AllStrategiesFailed) as excinfo:
        eng.fanout(strategies, prompt="x")
    assert len(excinfo.value.proposals) == 2
    assert all(p.error is not None for p in excinfo.value.proposals)


# ---------------------------------------------------------------------------
# 4. selector LLM fails -> structural-only fallback
# ---------------------------------------------------------------------------

def test_selector_llm_failure_falls_back_to_structural(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    info = _patch_call_model(
        monkeypatch,
        {
            "A": _GOOD_PLAN_A,   # high structural
            "B": _BAD_PLAN_C,    # near-zero structural
        },
        selector_response=RuntimeError("selector LLM exploded"),
    )
    strategies = [StrategySpec(provider="A"), StrategySpec(provider="B")]
    result = eng.fanout(strategies, prompt="x")
    assert info["selector_called"] == 1
    assert result.selector_method == "fallback"
    assert result.selector_error is not None and "exploded" in result.selector_error
    # structural alone should pick A (the well-structured plan)
    assert result.winner_index == 0


# ---------------------------------------------------------------------------
# 5. heuristic score components — length / fenced / steps / headers
# ---------------------------------------------------------------------------

def test_heuristic_scoring_components() -> None:
    # Empty
    s0 = _score_heuristic("")
    assert s0["length"] == 0.0 and s0["fenced"] == 0.0 and s0["structural"] == 0.0

    # Long plan with all the goodies
    s1 = _score_heuristic(_GOOD_PLAN_A)
    assert s1["length"] > 0.0
    assert s1["fenced"] == 1.0
    assert s1["steps"] > 0.0
    assert s1["headers"] > 0.0
    assert s1["structural"] > 0.5

    # Tiny plan -> low everything
    s2 = _score_heuristic("hi")
    assert s2["length"] == 0.0
    assert s2["fenced"] == 0.0
    assert s2["structural"] < 0.05


# ---------------------------------------------------------------------------
# 6. single proposal -> selector NOT called (short-circuit)
# ---------------------------------------------------------------------------

def test_single_proposal_short_circuits_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    info = _patch_call_model(monkeypatch, {"A": _GOOD_PLAN_A})
    result = eng.fanout([StrategySpec(provider="A")], prompt="x")
    assert info["selector_called"] == 0
    assert result.selector_method == "single"
    assert result.winner_index == 0
    assert result.winner.provider == "A"


# ---------------------------------------------------------------------------
# 7. weight tie-break — equal final score, higher weight wins
# ---------------------------------------------------------------------------

def test_weight_breaks_tie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    # Same plan text -> identical structural; selector returns equal LLM scores.
    _patch_call_model(
        monkeypatch,
        {"A": _GOOD_PLAN_A, "B": _GOOD_PLAN_A},
        selector_response={
            "winner_index": 0, "reason": "equal",
            "scores": [0.7, 0.7],
        },
    )
    strategies = [
        StrategySpec(provider="A", weight=1.0),
        StrategySpec(provider="B", weight=2.0),
    ]
    result = eng.fanout(strategies, prompt="x")
    # tie -> higher weight wins -> B (index 1)
    assert result.winner_index == 1
    assert result.winner.provider == "B"


# ---------------------------------------------------------------------------
# 8. tie-break with equal weights -> lower input index wins
# ---------------------------------------------------------------------------

def test_tie_breaks_to_lower_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(
        monkeypatch,
        {"A": _GOOD_PLAN_A, "B": _GOOD_PLAN_A},
        selector_response={
            "winner_index": 1, "reason": "equal",
            "scores": [0.7, 0.7],
        },
    )
    strategies = [
        StrategySpec(provider="A", weight=1.0),
        StrategySpec(provider="B", weight=1.0),
    ]
    result = eng.fanout(strategies, prompt="x")
    # equal weights, equal scores -> lower index (0) wins
    assert result.winner_index == 0
    assert result.winner.provider == "A"


# ---------------------------------------------------------------------------
# 9. empty strategies list -> ValueError
# ---------------------------------------------------------------------------

def test_empty_strategies_rejected(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    with pytest.raises(ValueError):
        eng.fanout([], prompt="x")


# ---------------------------------------------------------------------------
# 10. selection_to_dict shape
# ---------------------------------------------------------------------------

def test_selection_to_dict_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {"A": _GOOD_PLAN_A})
    result = eng.fanout([StrategySpec(provider="A")], prompt="x")
    d = selection_to_dict(result)
    assert d["winner_index"] == 0
    assert d["winner_provider"] == "A"
    assert d["selector_method"] == "single"
    assert isinstance(d["scores"], list) and len(d["scores"]) == 1
    pd = proposal_to_dict(result.proposals[0])
    assert pd["provider"] == "A"
    assert pd["text"] == _GOOD_PLAN_A
    assert pd["error"] is None


# ---------------------------------------------------------------------------
# 11. selector receives malformed JSON -> structural fallback
# ---------------------------------------------------------------------------

def test_selector_malformed_json_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(tmp_path)
    bad_resp = ModelResponse(
        text="not json {malformed",
        prompt_tokens=1, completion_tokens=1, cost_usd=0.0, latency_s=0.01, model="bad",
    )
    info = _patch_call_model(
        monkeypatch,
        {"A": _GOOD_PLAN_A, "B": _BAD_PLAN_C},
        selector_response=bad_resp,
    )
    strategies = [StrategySpec(provider="A"), StrategySpec(provider="B")]
    result = eng.fanout(strategies, prompt="x")
    assert info["selector_called"] == 1
    assert result.selector_method == "fallback"
    assert result.selector_error is not None
    # structural fallback -> A wins
    assert result.winner_index == 0
