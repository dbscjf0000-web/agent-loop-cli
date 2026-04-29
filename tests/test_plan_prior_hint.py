"""v0.7 Plan prompt enhancement: prior_judge_hint injection.

Covers:
  - ``_collect_prior_judge_hint`` returns "" on cycle 1 (no history yet).
  - It picks up the *most recent* judge hint from history.jsonl.
  - It falls back to ``artifacts/judge_result.json`` when history has no judge row.
  - It caps the hint length (default 1000 chars) with "..." suffix.
  - ``_run_plan_single`` actually passes the rendered hint into the LLM prompt
    (verified via call_model patching — no real LLM).

All tests mock the LLM. No live calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import workers
from agent_loop.config import Config
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir


def _write_history(td: TaskDir, rows: list[dict[str, Any]]) -> None:
    td.memory_dir().mkdir(parents=True, exist_ok=True)
    path = td.memory_dir() / "history.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------- _collect_prior_judge_hint ----------


def test_collect_prior_judge_hint_empty_when_no_history(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="aaa111")
    td.init()
    # No history.jsonl entries yet → cycle 1 semantics.
    assert workers._collect_prior_judge_hint(td) == ""


def test_collect_prior_judge_hint_returns_most_recent_judge_hint(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="bbb222")
    td.init()
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.7, "summary": "init"},
            {"cycle": 1, "phase": "judge",
             "summary": "perf weak", "hint": "expand-around-center O(n^2) → Manacher O(n)"},
            {"cycle": 2, "phase": "verify", "score": 0.7, "summary": "still 0.7"},
            {"cycle": 2, "phase": "judge",
             "summary": "still weak", "hint": "Replace with Manacher O(n) algorithm"},
        ],
    )
    out = workers._collect_prior_judge_hint(td)
    # Most recent (cycle 2) hint should be returned verbatim.
    assert out == "Replace with Manacher O(n) algorithm"


def test_collect_prior_judge_hint_falls_back_to_judge_result_json(tmp_path: Path) -> None:
    """When history has no judge row but judge_result.json exists, use that."""
    td = TaskDir(root=tmp_path, task_id="ccc333")
    td.init()
    # No judge row in history (e.g. older format / lost rows), but the artifact
    # is present — common after a crash + resume scenario.
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.5, "summary": "ok"},
        ],
    )
    td.write_artifact(
        "judge_result.json",
        {
            "better": False,
            "action": "redo_R",
            "reason": "perf still weak",
            "hint": "Use timeit + Manacher",
            "scores": {"this_cycle": 0.5, "best": None, "delta": None},
        },
    )
    out = workers._collect_prior_judge_hint(td)
    assert out == "Use timeit + Manacher"


def test_collect_prior_judge_hint_caps_length(tmp_path: Path) -> None:
    """A runaway hint must be truncated with a ``...`` suffix."""
    td = TaskDir(root=tmp_path, task_id="ddd444")
    td.init()
    long_hint = "X" * 5000
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "verify", "score": 0.5, "summary": "ok"},
            {"cycle": 1, "phase": "judge", "summary": "weak", "hint": long_hint},
        ],
    )
    out = workers._collect_prior_judge_hint(td, max_chars=200)
    assert len(out) <= 200
    assert out.endswith("...")


def test_collect_prior_judge_hint_skips_empty_hints(tmp_path: Path) -> None:
    """Judge rows with empty / missing hint are skipped — we want the latest *useful* one."""
    td = TaskDir(root=tmp_path, task_id="eee555")
    td.init()
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "judge", "summary": "ok",
             "hint": "go faster"},
            {"cycle": 2, "phase": "judge", "summary": "ok",
             "hint": ""},  # empty — must be skipped
            {"cycle": 3, "phase": "judge", "summary": "ok"},  # missing hint key
        ],
    )
    # Walking backward, cycle 3 has no hint, cycle 2's is empty → fall through
    # to cycle 1 ("go faster").
    out = workers._collect_prior_judge_hint(td)
    assert out == "go faster"


# ---------- _run_plan_single carries prior_judge_hint into the prompt ----------


def test_run_plan_single_passes_prior_judge_hint_to_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt rendered for the LLM must contain the prior judge hint string."""
    td = TaskDir(root=tmp_path, task_id="fff666")
    td.init()
    # Required artifacts for plan phase.
    td.task_md_path().write_text("Find the longest palindrome in s.", encoding="utf-8")
    td.write_artifact("findings.md", "Manacher O(n) is standard.")
    _write_history(
        td,
        [
            {"cycle": 1, "phase": "judge", "summary": "perf weak",
             "hint": "Use Manacher's algorithm O(n)"},
        ],
    )

    captured: dict[str, Any] = {}

    def fake_call_model(phase: str, prompt: str, **kwargs: Any) -> ModelResponse:
        captured["phase"] = phase
        captured["prompt"] = prompt
        return ModelResponse(
            text="# Plan\n\n## 0. 요약\n- mock\n",
            prompt_tokens=10,
            completion_tokens=10,
            cost_usd=0.0,
            latency_s=0.01,
            model="mock",
        )

    monkeypatch.setattr(workers, "call_model", fake_call_model)
    workers.run_plan(td, Config())

    assert captured.get("phase") == "plan"
    p = captured.get("prompt", "")
    # Both the section header and the hint itself must appear.
    assert "Prior Judge Hint" in p
    assert "Use Manacher's algorithm O(n)" in p
    # And the reasoning constraints must be present so the LLM is told to follow it.
    assert "Reasoning Constraints" in p


def test_run_plan_single_omits_prior_context_on_cycle_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cycle 1: no judge history → prior-context block is empty (no overhead).

    v0.7.2: dropping Reasoning Constraints + sentinel sections on cycle 1
    makes the prompt byte-identical to v0.6 there, avoiding the
    cursor-agent timeout we saw on hard tasks (sort_tuning) where the
    model wasted reasoning budget evaluating always-true sentinel
    conditions.
    """
    td = TaskDir(root=tmp_path, task_id="ggg777")
    td.init()
    td.task_md_path().write_text("dummy task", encoding="utf-8")
    td.write_artifact("findings.md", "dummy findings")

    captured: dict[str, Any] = {}

    def fake_call_model(phase: str, prompt: str, **kwargs: Any) -> ModelResponse:
        captured["prompt"] = prompt
        return ModelResponse(
            text="# Plan\n", prompt_tokens=1, completion_tokens=1,
            cost_usd=0.0, latency_s=0.01, model="mock",
        )

    monkeypatch.setattr(workers, "call_model", fake_call_model)
    workers.run_plan(td, Config())

    p = captured.get("prompt", "")
    # Sections must be absent on cycle 1 (zero v0.7 overhead when there
    # is no prior context to honor).
    assert "Prior Judge Hint" not in p
    assert "Prior Cycles" not in p
    assert "Reasoning Constraints" not in p
