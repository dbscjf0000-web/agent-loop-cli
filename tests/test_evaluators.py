"""Unit tests for the four evaluator backends.

All tests run against a temp ``TaskDir``; no real LLM is hit (the
``llm_rubric`` test stubs ``call_model``).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop.config import Config
from agent_loop.evaluators import evaluate_axis
from agent_loop.evaluators.ast_grep import run_ast_grep
from agent_loop.evaluators.benchmark import run_benchmark
from agent_loop.evaluators.llm_rubric import run_llm_rubric
from agent_loop.evaluators.pytest_runner import run_pytest
from agent_loop.state import TaskDir


def _td(tmp_path: Path, code: str) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id="t1")
    td.init()
    (td.workspace_path() / "solution.py").write_text(code, encoding="utf-8")
    return td


# ---------- pytest_runner ----------

def test_pytest_runner_all_pass(tmp_path: Path) -> None:
    td = _td(tmp_path, "def add(a, b):\n    return a + b\n")
    spec = {"weight": 1.0, "test": "assert add(1, 2) == 3\nassert add(0, 0) == 0\n"}
    score = run_pytest(name="correctness", spec=spec, task_dir=td, config=Config())
    assert score.score == 1.0
    assert score.is_ground_truth is True
    assert "2/2 assertions passed" in score.evidence


def test_pytest_runner_partial_fail(tmp_path: Path) -> None:
    td = _td(tmp_path, "def add(a, b):\n    return a + b\n")
    spec = {
        "weight": 1.0,
        "test": "assert add(1, 2) == 3\nassert add(2, 2) == 5\nassert add(0, 0) == 0\n",
    }
    score = run_pytest(name="correctness", spec=spec, task_dir=td, config=Config())
    assert 0 < score.score < 1
    assert score.raw["passed"] == 1
    assert score.raw["total"] == 3


def test_pytest_runner_import_error(tmp_path: Path) -> None:
    td = _td(tmp_path, "raise RuntimeError('boom')\n")
    spec = {"weight": 1.0, "test": "assert True"}
    score = run_pytest(name="correctness", spec=spec, task_dir=td, config=Config())
    assert score.score == 0.0
    assert "import failed" in score.evidence


# ---------- benchmark ----------

def test_benchmark_under_threshold(tmp_path: Path) -> None:
    td = _td(tmp_path, "def fast():\n    return sum(range(10))\n")
    spec = {
        "weight": 1.0,
        "stmt": "fast()",
        "threshold": 1.0,
        "repeats": 2,
        "measure": "wall_clock_seconds",
    }
    score = run_benchmark(name="performance", spec=spec, task_dir=td, config=Config())
    assert score.score == 1.0
    assert score.is_ground_truth is True
    assert "median=" in score.evidence


def test_benchmark_linear_decline(tmp_path: Path) -> None:
    # Force a slow stmt so the linear-falloff branch fires.
    td = _td(tmp_path, "import time\n\n\ndef slow():\n    time.sleep(0.05)\n")
    spec = {
        "weight": 1.0,
        "stmt": "slow()",
        "threshold": 0.01,  # median ~0.05 -> roughly 5x threshold -> 0
        "repeats": 1,
        "measure": "wall_clock_seconds",
    }
    score = run_benchmark(name="performance", spec=spec, task_dir=td, config=Config())
    assert score.score < 1.0


# ---------- ast_grep ----------

def test_ast_grep_pass(tmp_path: Path) -> None:
    td = _td(tmp_path, "def bs(a, t):\n    lo, hi = 0, len(a)-1\n    while lo <= hi:\n        m = (lo+hi)//2\n        return m\n")
    spec = {"weight": 1.0, "rule": "for _count<=1; .index( not_in"}
    score = run_ast_grep(name="complexity", spec=spec, task_dir=td, config=Config())
    assert score.score == 1.0
    assert score.is_ground_truth is True


def test_ast_grep_violation(tmp_path: Path) -> None:
    td = _td(tmp_path, "def bs(a, t):\n    return a.index(t) if t in a else -1\n")
    spec = {"weight": 1.0, "rule": ".index( not_in"}
    score = run_ast_grep(name="complexity", spec=spec, task_dir=td, config=Config())
    assert score.score < 1.0
    assert score.raw["violations"] == 1


def test_ast_grep_count_rule(tmp_path: Path) -> None:
    td = _td(tmp_path, "for x in a:\n    for y in b:\n        pass\n")
    spec = {"weight": 1.0, "rule": "for _count<=1"}
    score = run_ast_grep(name="complexity", spec=spec, task_dir=td, config=Config())
    assert score.score < 1.0


# ---------- llm_rubric ----------

def test_llm_rubric_monkeypatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = _td(tmp_path, "def f():\n    '''docstring.'''\n    return 1\n")

    def fake_call_model(phase: str, prompt: str, system: str = "", config: Any = None, workspace: Any = None, **_: Any):
        return SimpleNamespace(
            text='{"score": 0.85, "evidence": "well-documented"}',
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.0,
            latency_s=0.1,
            model="mock/test",
        )

    monkeypatch.setattr("agent_loop.evaluators.llm_rubric.call_model", fake_call_model, raising=False)
    # Inject the fake by placing it directly on the function module so the
    # late import inside run_llm_rubric resolves to it.
    import agent_loop.evaluators.llm_rubric as lr_mod
    monkeypatch.setattr(lr_mod, "call_model", fake_call_model, raising=False)
    # The late import lives inside run_llm_rubric — monkeypatch the source too.
    import agent_loop.models as mods
    monkeypatch.setattr(mods, "call_model", fake_call_model, raising=False)

    spec = {"weight": 0.5, "criterion": "is the code documented?"}
    score = run_llm_rubric(name="docs", spec=spec, task_dir=td, config=Config())
    assert score.score == pytest.approx(0.85)
    assert score.is_ground_truth is False
    assert "well-documented" in score.evidence


# ---------- evaluate_axis dispatch ----------

def test_evaluate_axis_unknown_evaluator(tmp_path: Path) -> None:
    td = _td(tmp_path, "x = 1\n")
    score = evaluate_axis("x", {"evaluator": "no_such", "weight": 0.5}, td, Config())
    assert score.score == 0.0
    assert "unknown evaluator" in score.evidence


def test_evaluate_axis_pytest(tmp_path: Path) -> None:
    td = _td(tmp_path, "def add(a, b):\n    return a + b\n")
    score = evaluate_axis(
        "correctness",
        {"evaluator": "pytest", "weight": 0.7, "test": "assert add(1, 1) == 2"},
        td,
        Config(),
    )
    assert score.score == 1.0
    assert score.weight == 0.7
    assert score.is_ground_truth is True
