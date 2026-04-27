"""Multi-axis Verify Engine (v0.2).

Replaces v0.1's single LLM verify call with a rubric-driven evaluation:
each axis is scored by a *programmatic* evaluator (pytest / benchmark /
ast_grep) when possible and an *LLM rubric* only as a soft fallback.

Public surface:
    VerifyEngine(task_dir, config).evaluate(rubric) -> VerifyResult
    yaml_to_rubric(success_criteria: list) -> dict   (benchmarks YAML helper)

Design contract:
- ``rubric`` is a dict ``{"axes": {axis_name: spec, ...}}``. Each spec
  carries ``weight`` + ``evaluator`` (or ``measure`` from a YAML).
- Ground-truth evaluators always win. The aggregate ``weighted_score`` is
  ``Σ(axis.score * axis.weight) / Σ(axis.weight)``.
- ``summary`` is capped at 200 chars to fit the Context Engine ``history.jsonl``
  one-line summary slot.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_loop.config import Config
from agent_loop.evaluators import evaluate_axis
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore, VerifyResult


_SUMMARY_CAP = 200


# ---------------------------------------------------------------------------
# YAML success_criteria -> rubric spec
# ---------------------------------------------------------------------------
def _infer_evaluator(criterion: dict[str, Any]) -> str:
    """Map a YAML benchmark axis -> evaluator name.

    Heuristic order:
      - ``measure: wall_clock_seconds`` / ``speedup_ratio``  -> benchmark
      - ``measure: source_inspection``                       -> ast_grep
      - presence of ``test:``                                 -> pytest
      - presence of ``rule:`` (no measure)                    -> ast_grep
      - else                                                  -> llm_rubric
    """
    measure = (criterion.get("measure") or "").strip().lower()
    if measure in {"wall_clock_seconds", "speedup_ratio"}:
        return "benchmark"
    if measure == "source_inspection":
        return "ast_grep"
    if criterion.get("test"):
        return "pytest"
    if criterion.get("rule"):
        return "ast_grep"
    return "llm_rubric"


def _benchmark_stmt_from_target(target: str) -> str | None:
    """Best-effort extract a Python expression to time from a target string.

    The YAML target reads like ``"n_queens_count(13) finishes in <= 1.5s"``
    — we grab the first parenthesised call. If we cannot, the spec must
    provide ``stmt`` explicitly.
    """
    if not target:
        return None
    # find first identifier(...) pattern
    import re

    m = re.search(r"([A-Za-z_]\w*\s*\([^)]*\))", target)
    return m.group(1).strip() if m else None


def yaml_to_rubric(success_criteria: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a benchmark yaml's ``success_criteria`` block into a rubric.

    Returns ``{"axes": {axis: spec, ...}}``. Spec keys are passed straight to
    the evaluator after a tiny normalisation step (extracting ``stmt`` from
    the human-readable ``target`` for benchmarks).
    """
    axes: dict[str, dict[str, Any]] = {}
    for crit in success_criteria or []:
        if not isinstance(crit, dict):
            continue
        axis = str(crit.get("axis") or crit.get("name") or "").strip()
        if not axis:
            continue
        evaluator = _infer_evaluator(crit)
        spec: dict[str, Any] = {
            "weight": float(crit.get("weight", 1.0) or 0.0),
            "evaluator": evaluator,
        }
        # forward common fields
        for key in ("test", "test_file", "rule", "criterion", "description",
                    "measure", "threshold", "repeats", "timeout",
                    "setup", "stmt", "baseline_stmt", "file"):
            if key in crit:
                spec[key] = crit[key]
        # benchmark: derive stmt from target if not explicit
        if evaluator == "benchmark" and "stmt" not in spec:
            stmt = _benchmark_stmt_from_target(str(crit.get("target") or ""))
            if stmt:
                spec["stmt"] = stmt
        axes[axis] = spec
    return {"axes": axes}


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class VerifyEngine:
    """Orchestrates per-axis evaluation against a rubric."""

    def __init__(self, task_dir: TaskDir, config: Config) -> None:
        self.task_dir = task_dir
        self.config = config

    def evaluate(
        self,
        rubric: dict[str, Any] | None = None,
        *,
        llm_fallback: bool = True,
    ) -> VerifyResult:
        """Run every axis in ``rubric`` and return a ``VerifyResult``.

        When ``rubric`` is ``None`` or has no axes, returns an empty result
        (caller is expected to fall back to legacy LLM verify).
        """
        axes_spec = (rubric or {}).get("axes") or {}
        scored: list[AxisScore] = []
        for name, spec in axes_spec.items():
            if not isinstance(spec, dict):
                continue
            if not llm_fallback and spec.get("evaluator") == "llm_rubric":
                continue
            scored.append(evaluate_axis(name, spec, self.task_dir, self.config).clamp())

        weighted = self._weighted(scored)
        summary = self._summary(scored, weighted)
        return VerifyResult(axes=scored, weighted_score=weighted, summary=summary)

    @staticmethod
    def _weighted(axes: list[AxisScore]) -> float:
        total_w = sum(max(0.0, a.weight) for a in axes)
        if total_w <= 0:
            return 0.0
        return round(sum(a.score * max(0.0, a.weight) for a in axes) / total_w, 6)

    @staticmethod
    def _summary(axes: list[AxisScore], weighted: float) -> str:
        if not axes:
            return "no axes evaluated"
        bits = " ".join(f"{a.name}={a.score:.2f}" for a in axes)
        out = f"{bits} -> {weighted:.3f}"
        return out if len(out) <= _SUMMARY_CAP else out[: _SUMMARY_CAP - 3] + "..."


def result_to_dict(result: VerifyResult) -> dict[str, Any]:
    """Serialise a ``VerifyResult`` to the on-disk ``solution.json`` schema."""
    return {
        "weighted_score": result.weighted_score,
        "summary": result.summary,
        "axes": [asdict(a) for a in result.axes],
    }


__all__ = ["VerifyEngine", "VerifyResult", "AxisScore", "yaml_to_rubric", "result_to_dict"]
