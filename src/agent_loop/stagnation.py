"""Stagnation detector — Phase 1.

같은 weighted_score가 N회 연속 반복되면 cycle 조기 종료.
local optima에 빠진 경우 무한 cycle 낭비를 차단한다.
"""
from __future__ import annotations

import os

DEFAULT_THRESHOLD = 2
EPSILON = 0.01  # score delta 미만이면 동일 점수로 간주


def is_disabled() -> bool:
    return os.environ.get("AGENT_LOOP_DISABLE_STAGNATION", "").lower() in {"1", "true", "yes"}


def is_stagnant(score_history: list[float], threshold: int = DEFAULT_THRESHOLD) -> bool:
    """최근 ``threshold + 1`` 점수가 모두 EPSILON 이내로 동일하면 True.

    threshold=2 → 같은 점수 3회(c1=c2=c3) 발견 시 정체.
    minimum 2 cycles 데이터 필요. 부족하면 False.
    """
    if is_disabled():
        return False
    if threshold < 1:
        return False
    needed = threshold + 1
    if len(score_history) < needed:
        return False
    recent = score_history[-needed:]
    return max(recent) - min(recent) < EPSILON


__all__ = ["is_stagnant", "is_disabled", "DEFAULT_THRESHOLD"]
