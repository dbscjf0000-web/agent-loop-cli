"""Phase 1 — stagnation detector unit tests."""
from __future__ import annotations

import os

import pytest

from agent_loop.stagnation import is_stagnant


def test_empty_history_not_stagnant() -> None:
    assert is_stagnant([]) is False


def test_single_score_not_stagnant() -> None:
    assert is_stagnant([0.6]) is False


def test_two_same_scores_threshold_2_not_stagnant() -> None:
    # threshold=2 → needs 3 cycles of same score
    assert is_stagnant([0.6, 0.6], threshold=2) is False


def test_three_same_scores_threshold_2_stagnant() -> None:
    assert is_stagnant([0.6, 0.6, 0.6], threshold=2) is True


def test_score_drift_within_epsilon_stagnant() -> None:
    # |max - min| < 0.01 still stagnant
    assert is_stagnant([0.601, 0.605, 0.600], threshold=2) is True


def test_score_drift_above_epsilon_not_stagnant() -> None:
    assert is_stagnant([0.60, 0.65, 0.70], threshold=2) is False


def test_only_recent_window_counts() -> None:
    # earlier varied, last 3 stagnant → True
    assert is_stagnant([0.1, 0.5, 0.8, 0.7, 0.7, 0.7], threshold=2) is True


def test_threshold_1_means_2_consecutive() -> None:
    assert is_stagnant([0.6, 0.6], threshold=1) is True
    assert is_stagnant([0.6, 0.65], threshold=1) is False


def test_env_disable_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOOP_DISABLE_STAGNATION", "1")
    assert is_stagnant([0.6, 0.6, 0.6], threshold=2) is False
