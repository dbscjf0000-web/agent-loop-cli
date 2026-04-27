"""yaml_to_rubric: ensure benchmark YAMLs convert to a usable rubric."""
from __future__ import annotations

from pathlib import Path

import yaml

from agent_loop.verify_engine import yaml_to_rubric


def _bench_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "benchmarks" / f"{name}.yaml"


def _load_criteria(name: str) -> list[dict]:
    spec = yaml.safe_load(_bench_path(name).read_text(encoding="utf-8"))
    return spec.get("success_criteria") or []


def test_n_queens_yaml_to_rubric_has_pytest_and_benchmark() -> None:
    crit = _load_criteria("n_queens")
    rubric = yaml_to_rubric(crit)
    axes = rubric["axes"]
    assert "correctness" in axes and "performance" in axes
    assert axes["correctness"]["evaluator"] == "pytest"
    assert axes["correctness"]["test"].strip().startswith("assert n_queens_count(1)")
    assert axes["performance"]["evaluator"] == "benchmark"
    # stmt should be inferred from the human-readable target
    assert "n_queens_count(13)" in axes["performance"]["stmt"]
    assert axes["performance"]["threshold"] == 1.5


def test_binary_search_yaml_has_ast_grep_axis() -> None:
    crit = _load_criteria("binary_search")
    rubric = yaml_to_rubric(crit)
    axes = rubric["axes"]
    assert "complexity" in axes
    assert axes["complexity"]["evaluator"] == "ast_grep"
    # weight from the YAML (0.3)
    assert axes["complexity"]["weight"] == 0.3


def test_sort_tuning_yaml_has_speedup_ratio() -> None:
    crit = _load_criteria("sort_tuning")
    rubric = yaml_to_rubric(crit)
    axes = rubric["axes"]
    assert axes["performance"]["evaluator"] == "benchmark"
    assert axes["performance"]["measure"] == "speedup_ratio"
    assert axes["performance"]["threshold"] == 0.9
