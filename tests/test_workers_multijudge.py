"""Tests for workers.run_judge multi-judge dispatch (v0.3).

Confirms:
  - single mode preserved when runtime.judges is None (back-compat).
  - multi mode + first-cycle short-circuit defers to single (no fan-out cost).
  - multi mode normal cycle writes consensus payload to judge_result.json.
  - all-fail falls back to single with consensus.fallback=True annotation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_loop import judge_engine as je_mod
from agent_loop import models as models_mod
from agent_loop import workers
from agent_loop.config import Config, JudgeSpec, Runtime
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


# ---------------------------------------------------------------------------
# 1. single mode preserved (runtime.judges = None)
# ---------------------------------------------------------------------------

def test_run_judge_single_mode_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="single1")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.7})
    td.write_artifact("best_solution.json", {"weighted_score": 0.5})

    payload = {"better": True, "action": "stop", "reason": "ok", "hint": "",
               "scores": {"this_cycle": 0.7, "best": 0.5, "delta": 0.2}}

    def fake_completion(**kw):
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
        )

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    cfg = Config()  # runtime.judges = None
    workers.run_judge(td, cfg)
    j = td.read_artifact("judge_result.json")
    assert isinstance(j, dict)
    # single mode -> no consensus key
    assert "consensus" not in j
    assert j["action"] == "stop"


# ---------------------------------------------------------------------------
# 2. multi mode + first cycle (no best_solution) -> defers to single, no fan-out
# ---------------------------------------------------------------------------

def test_run_judge_multi_first_cycle_no_fanout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="multi-first")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.97})
    # NO best_solution.json -> first-cycle short-circuit

    fan_out_calls = {"n": 0}

    def boom_call_model(*a, **kw):
        fan_out_calls["n"] += 1
        return _make_resp({})

    monkeypatch.setattr(je_mod, "call_model", boom_call_model)

    cfg = Config(runtime=Runtime(judges=[
        JudgeSpec(provider="claude/default"),
        JudgeSpec(provider="gemini/gemini-2.5-flash"),
    ]))
    resp = workers.run_judge(td, cfg)
    assert fan_out_calls["n"] == 0  # short-circuit -> JudgeEngine.call_model never invoked
    j = td.read_artifact("judge_result.json")
    assert "consensus" not in j  # single short-circuit path
    assert j["better"] is True
    assert resp.model.startswith("(skipped")


# ---------------------------------------------------------------------------
# 3. multi mode normal cycle -> consensus payload written
# ---------------------------------------------------------------------------

def test_run_judge_multi_normal_writes_consensus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="multi-normal")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.9})
    td.write_artifact("best_solution.json", {"weighted_score": 0.7})

    judge_calls = {"providers": []}

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        prov = config.models.judge if config else "?"
        judge_calls["providers"].append(prov)
        return _make_resp({
            "better": True,
            "action": "stop",
            "reason": f"ok from {prov}",
            "hint": f"hint-{prov}",
            "scores": {"this_cycle": 0.88},
        }, model=prov)

    monkeypatch.setattr(je_mod, "call_model", fake_call_model)

    cfg = Config(runtime=Runtime(judges=[
        JudgeSpec(provider="claude/default"),
        JudgeSpec(provider="gemini/gemini-2.5-flash"),
        JudgeSpec(provider="cursor/auto"),
    ]))
    workers.run_judge(td, cfg)

    assert sorted(judge_calls["providers"]) == [
        "claude/default", "cursor/auto", "gemini/gemini-2.5-flash",
    ]
    j = td.read_artifact("judge_result.json")
    assert "consensus" in j
    assert j["consensus"]["n_judges"] == 3
    assert j["consensus"]["votes_action"] == {"stop": 3.0}
    assert j["action"] == "stop"
    assert j["better"] is True
    assert j["scores"]["weighted"] == pytest.approx(0.88)
    # individual array preserved
    indiv = j["consensus"]["individual"]
    assert len(indiv) == 3
    assert all(i["error"] is None for i in indiv)


# ---------------------------------------------------------------------------
# 4. multi mode all-fail -> falls back to single with consensus.fallback=True
# ---------------------------------------------------------------------------

def test_run_judge_multi_all_fail_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="multi-allfail")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.6})
    td.write_artifact("best_solution.json", {"weighted_score": 0.5})

    # Fan-out judges all raise
    def fail_call_model(*a, **kw):
        raise RuntimeError("all CLI down")

    monkeypatch.setattr(je_mod, "call_model", fail_call_model)

    # Single-judge fallback uses litellm via models_mod.call_model
    fallback_payload = {
        "better": True, "action": "stop", "reason": "fallback ok", "hint": "",
        "scores": {"this_cycle": 0.6, "best": 0.5, "delta": 0.1},
    }

    def fake_completion(**kw):
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(fallback_payload)))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5),
        )

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    cfg = Config(runtime=Runtime(judges=[
        JudgeSpec(provider="claude/default"),
        JudgeSpec(provider="gemini/gemini-2.5-flash"),
    ]))
    workers.run_judge(td, cfg)

    j = td.read_artifact("judge_result.json")
    # single body wrote the canonical fields, then multi annotated consensus
    assert j["action"] == "stop"
    assert "consensus" in j
    assert j["consensus"]["fallback"] is True
    assert j["consensus"]["n_judges"] == 2
    # both individual entries should have error set
    errs = [i["error"] for i in j["consensus"]["individual"]]
    assert all(e is not None for e in errs)


# ---------------------------------------------------------------------------
# 5. config schema — list[str] form is normalized to JudgeSpec
# ---------------------------------------------------------------------------

def test_config_judges_normalize_str_list() -> None:
    cfg = Config.model_validate({
        "runtime": {"judges": ["claude/default", "gemini/gemini-2.5-flash"]}
    })
    assert cfg.runtime.judges is not None
    assert len(cfg.runtime.judges) == 2
    assert cfg.runtime.judges[0].provider == "claude/default"
    assert cfg.runtime.judges[0].weight == 1.0


def test_config_judges_empty_list_means_single() -> None:
    cfg = Config.model_validate({"runtime": {"judges": []}})
    # empty list -> normalized to None (single mode)
    assert cfg.runtime.judges is None


def test_config_judges_negative_weight_rejected() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError
        Config.model_validate({
            "runtime": {"judges": [{"provider": "x", "weight": 0}]}
        })


# ---------------------------------------------------------------------------
# 6. ENV var override — AGENT_LOOP_RUNTIME_JUDGES
# ---------------------------------------------------------------------------

def test_env_judges_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_loop.config import load_config

    monkeypatch.setenv(
        "AGENT_LOOP_RUNTIME_JUDGES",
        "claude/default, gemini/gemini-2.5-flash , cursor/auto",
    )
    cfg = load_config()
    assert cfg.runtime.judges is not None
    provs = [j.provider for j in cfg.runtime.judges]
    assert provs == ["claude/default", "gemini/gemini-2.5-flash", "cursor/auto"]
    assert all(j.weight == 1.0 for j in cfg.runtime.judges)
