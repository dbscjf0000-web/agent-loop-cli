"""Unit tests for the v0.4.1 auto-rubric generator.

Exercises:
- ``generate_rubric`` happy path (LLM mocked to return a valid JSON rubric).
- ``generate_rubric`` malformed JSON -> RuntimeError.
- ``_validate_and_normalize`` weights normalisation + axis count check.
- ``_validate_and_normalize`` rejects bad shapes (zero weight, missing
  criterion, single axis).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import auto_rubric
from agent_loop import models as models_mod
from agent_loop.config import Config


def _fake_completion(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: _fake_completion(response_text),
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)


# ---------- generate_rubric: happy path ----------

def test_generate_rubric_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "axes": {
            "correctness": {
                "weight": 0.6,
                "evaluator": "llm_rubric",
                "criterion": "returns correct gcd for all valid inputs",
            },
            "edge_cases": {
                "weight": 0.4,
                "evaluator": "llm_rubric",
                "criterion": "handles 0 and negatives correctly",
            },
        }
    }
    _patch_llm(monkeypatch, json.dumps(payload))

    gen = auto_rubric.generate_rubric("Implement gcd(a,b)", "(no findings)", Config())
    # v0.4.2: returns a RubricGeneration bundle, not a bare dict.
    assert isinstance(gen, auto_rubric.RubricGeneration)
    out = gen.rubric
    assert isinstance(out, dict)
    axes = out["axes"]
    assert set(axes.keys()) == {"correctness", "edge_cases"}
    # weights should still sum to ~1.0 after normalisation
    total = sum(spec["weight"] for spec in axes.values())
    assert total == pytest.approx(1.0, abs=1e-6)
    # evaluator forced to llm_rubric
    for spec in axes.values():
        assert spec["evaluator"] == "llm_rubric"
        assert isinstance(spec["criterion"], str) and spec["criterion"].strip()


# ---------- generate_rubric: malformed response ----------

def test_generate_rubric_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, "no JSON here at all, just prose")
    with pytest.raises(RuntimeError):
        auto_rubric.generate_rubric("task", "findings", Config())


def test_generate_rubric_fenced_block(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "axes": {
            "a1": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "criterion 1"},
            "a2": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "criterion 2"},
        }
    }
    text = "Here's the rubric:\n```json\n" + json.dumps(payload) + "\n```\nDone."
    _patch_llm(monkeypatch, text)
    gen = auto_rubric.generate_rubric("t", "f", Config())
    out = gen.rubric
    # weights normalised to 0.5 each (1.0 / 2.0)
    assert out["axes"]["a1"]["weight"] == pytest.approx(0.5)
    assert out["axes"]["a2"]["weight"] == pytest.approx(0.5)


# ---------- v0.4.2: ModelResponse preserved on RubricGeneration ----------

def test_generate_rubric_returns_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.4.2: response (cost/tokens/latency) is exposed via RubricGeneration."""
    payload = {
        "axes": {
            "correctness": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "c1"},
            "robustness": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "c2"},
        }
    }
    # Wire the mock LLM and override completion_cost to a known nonzero value
    # so we can verify it survived the pipeline rather than getting dropped.
    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: _fake_completion(json.dumps(payload)),
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0042)

    gen = auto_rubric.generate_rubric("task body", "findings body", Config())
    resp = gen.response
    # ModelResponse has the canonical fields and they are populated.
    assert hasattr(resp, "cost_usd")
    assert hasattr(resp, "latency_s")
    assert hasattr(resp, "prompt_tokens")
    assert hasattr(resp, "completion_tokens")
    assert resp.prompt_tokens == 10  # _fake_completion fixture
    assert resp.completion_tokens == 20
    assert resp.cost_usd == pytest.approx(0.0042)
    assert resp.latency_s >= 0.0  # nonzero clock — at least it was measured


# ---------- _validate_and_normalize ----------

def test_validate_normalizes_weights() -> None:
    rubric = {
        "axes": {
            "x": {"weight": 2.0, "evaluator": "llm_rubric", "criterion": "c1"},
            "y": {"weight": 8.0, "evaluator": "llm_rubric", "criterion": "c2"},
        }
    }
    auto_rubric._validate_and_normalize(rubric)
    assert rubric["axes"]["x"]["weight"] == pytest.approx(0.2)
    assert rubric["axes"]["y"]["weight"] == pytest.approx(0.8)


def test_validate_rejects_single_axis() -> None:
    rubric = {
        "axes": {
            "only": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "c"},
        }
    }
    with pytest.raises(ValueError, match="2 axes"):
        auto_rubric._validate_and_normalize(rubric)


def test_validate_rejects_zero_weight() -> None:
    rubric = {
        "axes": {
            "a": {"weight": 0.0, "evaluator": "llm_rubric", "criterion": "c"},
            "b": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "c"},
        }
    }
    with pytest.raises(RuntimeError, match="must be > 0"):
        auto_rubric._validate_and_normalize(rubric)


def test_validate_rejects_missing_criterion() -> None:
    rubric = {
        "axes": {
            "a": {"weight": 1.0, "evaluator": "llm_rubric"},
            "b": {"weight": 1.0, "evaluator": "llm_rubric", "criterion": "c"},
        }
    }
    with pytest.raises(RuntimeError, match="criterion"):
        auto_rubric._validate_and_normalize(rubric)


def test_validate_forces_llm_rubric_evaluator() -> None:
    """LLM may try evaluator='pytest' but we force llm_rubric (no code-aware yet)."""
    rubric = {
        "axes": {
            "a": {"weight": 1.0, "evaluator": "pytest", "criterion": "c"},
            "b": {"weight": 1.0, "evaluator": "benchmark", "criterion": "c"},
        }
    }
    auto_rubric._validate_and_normalize(rubric)
    for spec in rubric["axes"].values():
        assert spec["evaluator"] == "llm_rubric"


def test_validate_rejects_non_dict_axes() -> None:
    rubric = {"axes": "not a dict"}
    with pytest.raises(RuntimeError, match="axes"):
        auto_rubric._validate_and_normalize(rubric)
