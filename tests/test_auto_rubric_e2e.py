"""v0.4.1 — end-to-end mock for free-form auto-rubric flow.

Drives a free-form task through the orchestrator with all five phases
mocked. Asserts that:
  - Research writes BOTH ``findings.md`` and ``rubric_auto.json``.
  - Verify uses the auto-rubric path (multi-axis ``solution.json`` with
    ``axes`` as a *list* of axis-score dicts, not the legacy axes-dict).
  - Each auto axis is scored via ``llm_rubric`` (each call returns a
    well-formed score JSON).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config
from agent_loop.orchestrator import Orchestrator
from agent_loop.state import TaskDir


def _fake_completion(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=24),
    )


# A scripted set of LLM responses, one per call_model invocation.
_AUTO_RUBRIC_PAYLOAD = json.dumps({
    "axes": {
        "correctness": {
            "weight": 0.5,
            "evaluator": "llm_rubric",
            "criterion": "function returns the correct gcd for typical inputs",
        },
        "edge_cases": {
            "weight": 0.3,
            "evaluator": "llm_rubric",
            "criterion": "handles zero and negative inputs gracefully",
        },
        "code_quality": {
            "weight": 0.2,
            "evaluator": "llm_rubric",
            "criterion": "uses Euclidean algorithm idiomatically",
        },
    }
})


def test_free_form_task_uses_auto_rubric_in_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock e2e: a free-form prompt produces solution.json with 3 auto axes."""
    td = TaskDir(root=tmp_path / ".agent_loop", task_id="e2e-auto")
    td.init()

    # Disable cross_task_memory to keep the test hermetic (no ~/.agent-loop writes).
    cfg = Config()
    cfg.runtime.cross_task_memory = False
    cfg.runtime.max_redo = 1
    cfg.runtime.max_cycles = 1
    assert cfg.runtime.auto_rubric is True

    # Track call order. Phases in cycle 1:
    #   1) research findings  -> "# Findings ..."
    #   2) auto_rubric        -> JSON rubric (3 axes)
    #   3) plan               -> "# Plan ..."
    #   4) implement          -> python fenced
    #   5) llm_rubric correctness -> {"score":0.9, ...}
    #   6) llm_rubric edge_cases  -> {"score":0.7, ...}
    #   7) llm_rubric code_quality-> {"score":0.8, ...}
    #   8) judge              -> first-cycle short-circuit (no LLM call)
    scripted: list[str] = [
        "# Findings\n- gcd is well-known\n- use Euclidean algorithm\n",
        _AUTO_RUBRIC_PAYLOAD,
        "# Plan\n1. write gcd function\n2. handle edge cases\n",
        "## notes\nimplementing gcd\n```python\n"
        "def gcd(a, b):\n    while b:\n        a, b = b, a % b\n    return abs(a)\n"
        "```\n",
        json.dumps({"score": 0.9, "evidence": "passes typical inputs"}),
        json.dumps({"score": 0.7, "evidence": "abs handles negatives"}),
        json.dumps({"score": 0.8, "evidence": "idiomatic euclidean"}),
    ]
    counter = {"i": 0}

    def fake(**kw: Any) -> Any:
        i = counter["i"]
        counter["i"] += 1
        text = scripted[i] if i < len(scripted) else "{}"
        return _fake_completion(text)

    monkeypatch.setattr(models_mod.litellm, "completion", fake)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    orch = Orchestrator(td, cfg)
    result = orch.run(task="Implement gcd(a, b)", max_cycles=1, mode="auto", max_redo=1)

    # Sanity: ran 1 cycle
    assert result["cycles_run"] == 1

    # Research wrote rubric_auto.json
    assert td.has_artifact("rubric_auto.json")
    rubric = td.read_artifact("rubric_auto.json")
    assert isinstance(rubric, dict)
    assert set(rubric["axes"].keys()) == {"correctness", "edge_cases", "code_quality"}

    # Verify used auto-rubric path -> v0.2 schema (axes is a list of dicts)
    sol = td.read_artifact("solution.json")
    assert isinstance(sol, dict)
    axes = sol.get("axes")
    assert isinstance(axes, list)
    axis_names = sorted(a["name"] for a in axes)
    assert axis_names == ["code_quality", "correctness", "edge_cases"]
    # weighted_score should reflect the mocked rubric scores
    # 0.9*0.5 + 0.7*0.3 + 0.8*0.2 = 0.45 + 0.21 + 0.16 = 0.82
    assert sol["weighted_score"] == pytest.approx(0.82, abs=0.01)

    # All 3 auto axes were scored via llm_rubric (not ground-truth)
    for a in axes:
        assert a["evaluator"] == "llm_rubric"
        assert a["is_ground_truth"] is False
