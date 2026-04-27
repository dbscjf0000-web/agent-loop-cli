"""Unit tests for v0.3 JudgeEngine — cross-vendor consensus.

Every test mocks ``call_model`` so no real CLI / litellm is invoked.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_loop import judge_engine as je_mod
from agent_loop.config import Config, JudgeSpec, Runtime
from agent_loop.judge_engine import (
    AllJudgesFailed,
    ConsensusResult,
    IndividualJudgement,
    JudgeEngine,
    consensus_to_dict,
)
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir


def _make_resp(payload: dict[str, Any], model: str = "mock") -> ModelResponse:
    return ModelResponse(
        text=json.dumps(payload),
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.0,
        latency_s=0.05,
        model=model,
    )


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id="judge-engine")
    td.init()
    return td


def _engine(tmp_path: Path) -> JudgeEngine:
    cfg = Config()
    return JudgeEngine(_td(tmp_path), cfg)


def _patch_call_model(monkeypatch: pytest.MonkeyPatch, by_provider: dict[str, Any]) -> dict[str, int]:
    """Patch judge_engine.call_model with a per-provider router. Returns counter dict."""
    calls = {"n": 0}

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        calls["n"] += 1
        prov = config.models.judge if config else "?"
        spec = by_provider.get(prov)
        if spec is None:
            raise RuntimeError(f"no mock for provider={prov}")
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, ModelResponse):
            return spec
        # treat dict as payload
        return _make_resp(spec, model=prov)

    monkeypatch.setattr(je_mod, "call_model", fake_call_model)
    return calls


# ---------------------------------------------------------------------------
# 1. happy path — 3 judges all stop / better=true
# ---------------------------------------------------------------------------

def test_consensus_three_judges_all_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "claude/default": {"better": True, "action": "stop", "reason": "good", "hint": "h1",
                           "scores": {"this_cycle": 0.9}},
        "gemini/gemini-2.5-flash": {"better": True, "action": "stop", "reason": "ok", "hint": "h2",
                                     "scores": {"this_cycle": 0.85}},
        "cursor/auto": {"better": True, "action": "stop", "reason": "fine", "hint": "",
                        "scores": {"this_cycle": 0.8}},
    })
    judges = [
        JudgeSpec(provider="claude/default"),
        JudgeSpec(provider="gemini/gemini-2.5-flash"),
        JudgeSpec(provider="cursor/auto"),
    ]
    result = eng.consensus(judges, prompt="judge me")
    assert result.better is True
    assert result.action == "stop"
    assert result.n_judges == 3
    assert result.votes_action == {"stop": 3.0}
    assert result.votes_better == {"true": 3.0, "false": 0.0}
    # weighted avg of 0.9 + 0.85 + 0.8 (all weight 1)
    assert result.scores["weighted"] == pytest.approx((0.9 + 0.85 + 0.8) / 3.0)


# ---------------------------------------------------------------------------
# 2. action majority 2:1
# ---------------------------------------------------------------------------

def test_action_majority_two_to_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop", "scores": {"this_cycle": 0.9}},
        "B": {"better": True, "action": "stop", "scores": {"this_cycle": 0.85}},
        "C": {"better": False, "action": "redo_P", "scores": {"this_cycle": 0.5}},
    })
    judges = [JudgeSpec(provider=p) for p in ("A", "B", "C")]
    result = eng.consensus(judges, prompt="x")
    assert result.action == "stop"
    assert result.votes_action == {"stop": 2.0, "redo_P": 1.0}


# ---------------------------------------------------------------------------
# 3. action tie -> stop preferred (conservative)
# ---------------------------------------------------------------------------

def test_action_tie_prefers_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop", "scores": {"this_cycle": 0.9}},
        "B": {"better": False, "action": "redo_P", "scores": {"this_cycle": 0.5}},
    })
    judges = [JudgeSpec(provider="A"), JudgeSpec(provider="B")]
    result = eng.consensus(judges, prompt="x")
    assert result.action == "stop"


# ---------------------------------------------------------------------------
# 4. action tie without stop -> alphabetic first (redo_P < redo_R)
# ---------------------------------------------------------------------------

def test_action_tie_no_stop_alphabetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": False, "action": "redo_R"},
        "B": {"better": False, "action": "redo_P"},
    })
    judges = [JudgeSpec(provider="A"), JudgeSpec(provider="B")]
    result = eng.consensus(judges, prompt="x")
    # 'redo_P' sorts before 'redo_R'
    assert result.action == "redo_P"


# ---------------------------------------------------------------------------
# 5. weight applied — A weight=2 redo_P, B+C weight=1 stop -> tie -> stop
# ---------------------------------------------------------------------------

def test_weight_breaks_tie_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": False, "action": "redo_P"},
        "B": {"better": True, "action": "stop"},
        "C": {"better": True, "action": "stop"},
    })
    judges = [
        JudgeSpec(provider="A", weight=2.0),
        JudgeSpec(provider="B", weight=1.0),
        JudgeSpec(provider="C", weight=1.0),
    ]
    result = eng.consensus(judges, prompt="x")
    # votes_action: stop=2.0, redo_P=2.0 -> tie -> stop
    assert result.action == "stop"
    assert result.votes_action == {"redo_P": 2.0, "stop": 2.0}


# ---------------------------------------------------------------------------
# 6. partial failure — one judge raises, others succeed
# ---------------------------------------------------------------------------

def test_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop", "scores": {"this_cycle": 0.9}},
        "B": RuntimeError("CLI timeout after 600s"),
        "C": {"better": True, "action": "stop", "scores": {"this_cycle": 0.8}},
    })
    judges = [JudgeSpec(provider=p) for p in ("A", "B", "C")]
    result = eng.consensus(judges, prompt="x")
    assert result.n_judges == 3
    errors = [i.error for i in result.individual]
    assert errors[0] is None
    assert errors[1] is not None and "CLI timeout" in errors[1]
    assert errors[2] is None
    # only 2 valid -> action=stop with weight 2
    assert result.action == "stop"
    assert result.votes_action == {"stop": 2.0}


# ---------------------------------------------------------------------------
# 7. all judges fail -> AllJudgesFailed
# ---------------------------------------------------------------------------

def test_all_fail_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": RuntimeError("a fail"),
        "B": RuntimeError("b fail"),
        "C": RuntimeError("c fail"),
    })
    judges = [JudgeSpec(provider=p) for p in ("A", "B", "C")]
    with pytest.raises(AllJudgesFailed) as excinfo:
        eng.consensus(judges, prompt="x")
    assert len(excinfo.value.individuals) == 3
    assert all(i.error is not None for i in excinfo.value.individuals)


# ---------------------------------------------------------------------------
# 8. weighted score average with weights
# ---------------------------------------------------------------------------

def test_weighted_score_average(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop", "scores": {"this_cycle": 0.9}},
        "B": {"better": True, "action": "stop", "scores": {"this_cycle": 0.6}},
    })
    judges = [
        JudgeSpec(provider="A", weight=2.0),
        JudgeSpec(provider="B", weight=1.0),
    ]
    result = eng.consensus(judges, prompt="x")
    # (2*0.9 + 1*0.6) / 3 = 0.8
    assert result.scores["weighted"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 9. score None mix — one judge omits scores
# ---------------------------------------------------------------------------

def test_score_none_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop"},  # no scores
        "B": {"better": True, "action": "stop", "scores": {"this_cycle": 0.7}},
    })
    judges = [JudgeSpec(provider="A"), JudgeSpec(provider="B")]
    result = eng.consensus(judges, prompt="x")
    # only B has a score
    assert result.scores["weighted"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 10. better tie -> False (conservative)
# ---------------------------------------------------------------------------

def test_better_tie_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop"},
        "B": {"better": False, "action": "stop"},
    })
    judges = [JudgeSpec(provider="A"), JudgeSpec(provider="B")]
    result = eng.consensus(judges, prompt="x")
    assert result.better is False
    assert result.votes_better == {"true": 1.0, "false": 1.0}


# ---------------------------------------------------------------------------
# 11. unparseable JSON from one judge -> error captured, others continue
# ---------------------------------------------------------------------------

def test_unparseable_json_recorded_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    bad_resp = ModelResponse(
        text="this is not json at all",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
        latency_s=0.01,
        model="bad",
    )
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop"},
        "B": bad_resp,
    })
    judges = [JudgeSpec(provider="A"), JudgeSpec(provider="B")]
    result = eng.consensus(judges, prompt="x")
    # A succeeded
    assert result.individual[0].error is None
    # B parsed as error
    assert result.individual[1].error is not None
    assert "unparseable" in result.individual[1].error.lower()
    # consensus driven by A
    assert result.action == "stop"


# ---------------------------------------------------------------------------
# 12. consensus_to_dict shape
# ---------------------------------------------------------------------------

def test_consensus_to_dict_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    _patch_call_model(monkeypatch, {
        "A": {"better": True, "action": "stop", "hint": "h", "reason": "r",
              "scores": {"this_cycle": 0.9}},
    })
    result = eng.consensus([JudgeSpec(provider="A")], prompt="x")
    d = consensus_to_dict(result)
    assert d["n_judges"] == 1
    assert d["fallback"] is False
    assert isinstance(d["individual"], list) and len(d["individual"]) == 1
    assert d["individual"][0]["provider"] == "A"
    assert d["individual"][0]["weighted_score"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 13. empty judges list -> ValueError
# ---------------------------------------------------------------------------

def test_empty_judges_list_rejected(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    with pytest.raises(ValueError):
        eng.consensus([], prompt="x")
