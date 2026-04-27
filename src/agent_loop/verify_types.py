"""Shared dataclasses for the v0.2 Verify Engine + evaluator backends.

Kept in its own module so ``verify_engine.py`` and ``evaluators/*`` can both
import them without a circular dependency through the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AxisScore:
    """One rubric axis result. Returned by every evaluator."""

    name: str
    score: float                 # always normalised to [0, 1]
    weight: float
    evaluator: str               # "pytest" | "benchmark" | "ast_grep" | "llm_rubric"
    evidence: str                # short, human-readable
    is_ground_truth: bool        # programmatic evaluator -> True
    raw: dict[str, Any] | None = None  # detailed payload (timings, counts, ...)

    def clamp(self) -> "AxisScore":
        """Clip ``score`` into [0, 1] in place. Returns self for chaining."""
        if self.score < 0.0:
            self.score = 0.0
        elif self.score > 1.0:
            self.score = 1.0
        return self


@dataclass
class VerifyResult:
    """VerifyEngine.evaluate() return type. Serialised to ``solution.json``."""

    axes: list[AxisScore] = field(default_factory=list)
    weighted_score: float = 0.0
    summary: str = ""           # 200-char cap (Context Engine constraint)


__all__ = ["AxisScore", "VerifyResult"]
