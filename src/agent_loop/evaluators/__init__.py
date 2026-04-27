"""Evaluator registry for the v0.2 multi-axis Verify Engine.

Each evaluator turns a rubric axis spec into an ``AxisScore`` (0..1 score +
weight + evidence). Programmatic evaluators (pytest / benchmark / ast_grep)
are *ground truth* — VerifyEngine trusts them over LLM-rubric evaluators.

Public API:
    EVALUATORS               registry: name -> callable
    evaluate_axis(name,      dispatch a single axis spec to its evaluator
                  spec, ...)
"""
from __future__ import annotations

from typing import Any, Callable

from agent_loop.config import Config
from agent_loop.evaluators.ast_grep import run_ast_grep
from agent_loop.evaluators.benchmark import run_benchmark
from agent_loop.evaluators.llm_rubric import run_llm_rubric
from agent_loop.evaluators.pytest_runner import run_pytest
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore


# Order matters only for stable error messages; lookup is by name.
EVALUATORS: dict[str, Callable[..., AxisScore]] = {
    "pytest": run_pytest,
    "benchmark": run_benchmark,
    "ast_grep": run_ast_grep,
    "llm_rubric": run_llm_rubric,
}


_GROUND_TRUTH = {"pytest", "benchmark", "ast_grep"}


def evaluate_axis(
    name: str,
    spec: dict[str, Any],
    task_dir: TaskDir,
    config: Config,
) -> AxisScore:
    """Dispatch an axis spec to its evaluator and return an ``AxisScore``.

    ``spec["evaluator"]`` selects the function. Unknown evaluators yield a
    score of 0 with diagnostic evidence rather than raising — the orchestrator
    should never crash because of a bad rubric.
    """
    evaluator = str(spec.get("evaluator") or "llm_rubric")
    fn = EVALUATORS.get(evaluator)
    weight = float(spec.get("weight", 1.0) or 0.0)
    if fn is None:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator=evaluator,
            evidence=f"unknown evaluator: {evaluator!r}",
            is_ground_truth=False,
            raw=None,
        )
    try:
        score = fn(name=name, spec=spec, task_dir=task_dir, config=config)
    except Exception as exc:  # never let a single axis crash the run
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator=evaluator,
            evidence=f"{type(exc).__name__}: {exc}",
            is_ground_truth=evaluator in _GROUND_TRUTH,
            raw=None,
        )
    # Normalise weight + ground-truth flag in case the evaluator forgot.
    score.weight = weight if score.weight is None or score.weight == 0 else score.weight
    score.is_ground_truth = evaluator in _GROUND_TRUTH
    score.evaluator = evaluator
    score.name = name
    return score


__all__ = ["EVALUATORS", "evaluate_axis"]
