"""Tests for v0.3.1 ``runtime.judge_always_llm`` switch.

When set, the first-cycle short-circuit (which skips the judge LLM when no
``best_solution.json`` exists) is disabled. The judge LLM (single or multi)
is invoked even on cycle 1, with an empty ``best_solution`` stub. This is
required for genuine multi-judge cross-vendor verification on tasks that
return weighted_score>=0.95 on cycle 1.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_loop import judge_engine as je_mod
from agent_loop import models as models_mod
from agent_loop import workers
from agent_loop.config import Config, JudgeSpec, Runtime, load_config
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir


def _resp(payload: dict[str, Any], model: str = "mock") -> ModelResponse:
    return ModelResponse(
        text=json.dumps(payload),
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
        latency_s=0.01,
        model=model,
    )


# ---------------------------------------------------------------------------
# 1. Default (judge_always_llm=False) -> first-cycle short-circuit preserved
# ---------------------------------------------------------------------------

def test_default_first_cycle_short_circuit_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="default-first")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.97})
    # No best_solution.json -> first cycle

    n_calls = {"n": 0}

    def boom(*a, **kw):
        n_calls["n"] += 1
        raise AssertionError("LLM should not be called on first cycle by default")

    monkeypatch.setattr(models_mod, "call_model", boom)

    cfg = Config()  # judge_always_llm = False (default)
    resp = workers.run_judge(td, cfg)

    assert n_calls["n"] == 0  # short-circuit -> no LLM call
    assert resp.model.startswith("(skipped")
    j = td.read_artifact("judge_result.json")
    assert "consensus" not in j  # single skip path
    assert j["better"] is True
    assert j["reason"].startswith("no prior best")


# ---------------------------------------------------------------------------
# 2. judge_always_llm=True (single mode) -> LLM IS called on first cycle
# ---------------------------------------------------------------------------

def test_judge_always_llm_single_invokes_llm_on_first_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="always-single")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.97})
    # No best_solution.json -> normally first-cycle short-circuit

    calls: list[dict[str, Any]] = []

    def fake_call(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        calls.append({"phase": phase, "prompt_len": len(prompt)})
        return _resp(
            {
                "better": True,
                "action": "stop",
                "reason": "first-cycle judged via LLM",
                "hint": "",
                "scores": {"this_cycle": 0.97, "best": None, "delta": None},
            },
            model="anthropic/claude-haiku-4-5",
        )

    # workers._run_judge_single uses workers.call_model (re-exported), so patch there
    monkeypatch.setattr(workers, "call_model", fake_call)

    cfg = Config(runtime=Runtime(judge_always_llm=True))
    resp = workers.run_judge(td, cfg)

    assert len(calls) == 1  # short-circuit disabled, LLM called
    assert calls[0]["phase"] == "judge"
    j = td.read_artifact("judge_result.json")
    assert j["reason"] == "first-cycle judged via LLM"
    assert j["action"] == "stop"
    assert resp.text  # not skipped


# ---------------------------------------------------------------------------
# 3. judge_always_llm=True (multi mode) -> all judges fan out on first cycle
# ---------------------------------------------------------------------------

def test_judge_always_llm_multi_fans_out_on_first_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="always-multi")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.97})
    # No best_solution.json -> normally first-cycle short-circuit -> defer to single

    seen: list[str] = []

    def fake_judge_call(phase: str, prompt: str, system: str = "", config: Any = None, **kw: Any):
        prov = config.models.judge if config else "?"
        seen.append(prov)
        return _resp(
            {
                "better": True,
                "action": "stop",
                "reason": f"first-cycle judged by {prov}",
                "hint": "",
                "scores": {"this_cycle": 0.97},
            },
            model=prov,
        )

    monkeypatch.setattr(je_mod, "call_model", fake_judge_call)

    cfg = Config(
        runtime=Runtime(
            judge_always_llm=True,
            judges=[
                JudgeSpec(provider="claude/default"),
                JudgeSpec(provider="gemini/gemini-2.5-flash"),
            ],
        )
    )
    workers.run_judge(td, cfg)

    assert sorted(seen) == ["claude/default", "gemini/gemini-2.5-flash"]
    j = td.read_artifact("judge_result.json")
    assert "consensus" in j  # multi-judge consensus payload was written
    assert j["consensus"]["n_judges"] == 2
    assert j["action"] == "stop"


# ---------------------------------------------------------------------------
# 4. judge_always_llm=False (multi mode) -> first cycle still short-circuits
# ---------------------------------------------------------------------------

def test_judge_always_llm_false_multi_still_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = TaskDir(root=tmp_path, task_id="default-multi")
    td.init()
    td.task_md_path().write_text("task", encoding="utf-8")
    td.write_artifact("solution.json", {"weighted_score": 0.97})

    n_calls = {"n": 0}

    def boom(*a, **kw):
        n_calls["n"] += 1
        raise AssertionError("multi-judge should not fan out on first cycle by default")

    monkeypatch.setattr(je_mod, "call_model", boom)

    cfg = Config(
        runtime=Runtime(
            judges=[
                JudgeSpec(provider="claude/default"),
                JudgeSpec(provider="gemini/gemini-2.5-flash"),
            ],
        )
    )
    # default: judge_always_llm = False
    workers.run_judge(td, cfg)
    assert n_calls["n"] == 0
    j = td.read_artifact("judge_result.json")
    assert "consensus" not in j  # short-circuited via single path


# ---------------------------------------------------------------------------
# 5. ENV override: AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM
# ---------------------------------------------------------------------------

def test_env_judge_always_llm_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM", "true")
    cfg = load_config()
    assert cfg.runtime.judge_always_llm is True

    monkeypatch.setenv("AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM", "0")
    cfg = load_config()
    assert cfg.runtime.judge_always_llm is False


# ---------------------------------------------------------------------------
# 6. CLI flag plumbing — `_override_runtime_v031(judge_always_llm=True)`
# ---------------------------------------------------------------------------

def test_cli_flag_judge_always_llm_lands_in_runtime() -> None:
    from agent_loop.cli import _override_runtime_v031

    cfg = Config()
    assert cfg.runtime.judge_always_llm is False
    out = _override_runtime_v031(
        cfg,
        cli_timeout=None,
        cli_timeout_verify=None,
        cli_timeout_judge=None,
        judge_always_llm=True,
    )
    assert out.runtime.judge_always_llm is True
