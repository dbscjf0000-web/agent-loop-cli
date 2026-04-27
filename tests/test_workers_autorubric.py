"""v0.4.1 — workers integration with auto_rubric.

Exercises:
- ``run_research`` writes ``rubric_auto.json`` when ``runtime.auto_rubric=True``
  and no rubric.json is present.
- ``run_research`` skips auto_rubric when ``runtime.auto_rubric=False``.
- ``run_research`` skips auto_rubric when ``rubric.json`` already exists
  (yaml-driven bench).
- ``run_verify`` priority: rubric.json > rubric_auto.json > legacy LLM.
- LLM parse failure during auto_rubric does NOT block research (graceful skip).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop import workers
from agent_loop.config import Config
from agent_loop.state import TaskDir


def _fake_completion(text: str, pt: int = 5, ct: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


def _seed(tmp_path: Path, tid: str = "wa1") -> TaskDir:
    td = TaskDir(root=tmp_path, task_id=tid)
    td.init()
    td.task_md_path().write_text("Implement gcd(a, b)", encoding="utf-8")
    return td


_VALID_RUBRIC_JSON = json.dumps({
    "axes": {
        "correctness": {
            "weight": 0.6,
            "evaluator": "llm_rubric",
            "criterion": "returns correct gcd",
        },
        "edge_cases": {
            "weight": 0.4,
            "evaluator": "llm_rubric",
            "criterion": "handles 0/negatives",
        },
    }
})


def _scripted_completion(monkeypatch: pytest.MonkeyPatch, scripted: list[str]) -> dict:
    """Return a counter; each call to litellm.completion pops next scripted text."""
    counter = {"i": 0}

    def fake(**kw: Any) -> Any:
        i = counter["i"]
        counter["i"] += 1
        text = scripted[i] if i < len(scripted) else "(default)"
        return _fake_completion(text)

    monkeypatch.setattr(models_mod.litellm, "completion", fake)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)
    return counter


# ---------- run_research auto-rubric path ----------

def test_run_research_writes_rubric_auto_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _seed(tmp_path)
    # First call = research findings, second call = auto_rubric JSON
    _scripted_completion(monkeypatch, ["# Findings\n- gcd is well-known", _VALID_RUBRIC_JSON])

    cfg = Config()
    assert cfg.runtime.auto_rubric is True  # default-ON sanity
    workers.run_research(td, cfg)

    assert td.has_artifact("findings.md")
    assert td.has_artifact("rubric_auto.json")
    rubric = td.read_artifact("rubric_auto.json")
    assert isinstance(rubric, dict)
    assert set(rubric["axes"].keys()) == {"correctness", "edge_cases"}


def test_run_research_skips_auto_rubric_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _seed(tmp_path, tid="wa2")
    counter = _scripted_completion(monkeypatch, ["# Findings\n- gcd"])

    cfg = Config()
    cfg.runtime.auto_rubric = False
    workers.run_research(td, cfg)

    assert td.has_artifact("findings.md")
    assert not td.has_artifact("rubric_auto.json")
    # Only 1 LLM call (no auto-rubric)
    assert counter["i"] == 1


def test_run_research_skips_when_rubric_json_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yaml-driven bench (rubric.json) -> auto_rubric is skipped (backward compat)."""
    td = _seed(tmp_path, tid="wa3")
    td.write_artifact("rubric.json", {"axes": {"x": {"weight": 1.0, "evaluator": "pytest"}}})
    counter = _scripted_completion(monkeypatch, ["# Findings only"])

    workers.run_research(td, Config())

    # auto generation skipped
    assert not td.has_artifact("rubric_auto.json")
    # single LLM call (research only)
    assert counter["i"] == 1


def test_run_research_graceful_on_auto_rubric_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _seed(tmp_path, tid="wa4")
    # research returns prose; auto_rubric also gets unparseable text -> skipped silently
    _scripted_completion(monkeypatch, ["# Findings prose", "this is NOT json"])

    workers.run_research(td, Config())  # should not raise

    assert td.has_artifact("findings.md")
    assert not td.has_artifact("rubric_auto.json")


# ---------- run_verify priority order ----------

def test_run_verify_prefers_rubric_over_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rubric.json (yaml) wins over rubric_auto.json (research)."""
    td = _seed(tmp_path, tid="wv1")
    (td.workspace_path() / "solution.py").write_text("def gcd(a,b): return 0\n", encoding="utf-8")

    td.write_artifact("rubric.json", {
        "axes": {
            "x": {
                "evaluator": "ast_grep",
                "weight": 1.0,
                "rule": "gcd in",
            }
        }
    })
    td.write_artifact("rubric_auto.json", {
        "axes": {
            "y": {
                "evaluator": "llm_rubric",
                "weight": 1.0,
                "criterion": "ignored",
            }
        }
    })
    # No LLM calls expected (rubric.json axis is ast_grep, no LLM needed)
    called = {"n": 0}
    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: (called.__setitem__("n", called["n"] + 1) or _fake_completion("{}"))
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    workers.run_verify(td, Config())
    sol = td.read_artifact("solution.json")
    assert isinstance(sol, dict)
    # solution.json axes correspond to rubric.json's axis "x", not rubric_auto's "y"
    axes_names = [a.get("name") for a in sol.get("axes", [])]
    assert "x" in axes_names
    assert "y" not in axes_names


def test_run_verify_falls_back_to_auto_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rubric.json + auto_rubric=True + rubric_auto.json present -> use it."""
    td = _seed(tmp_path, tid="wv2")
    (td.workspace_path() / "solution.py").write_text("def gcd(a,b): return a\n", encoding="utf-8")
    # Use ast_grep so no LLM call is needed (deterministic test)
    td.write_artifact("rubric_auto.json", {
        "axes": {
            "auto_axis_1": {"evaluator": "ast_grep", "weight": 0.5, "rule": "gcd in"},
            "auto_axis_2": {"evaluator": "ast_grep", "weight": 0.5, "rule": "def in"},
        }
    })

    workers.run_verify(td, Config())
    sol = td.read_artifact("solution.json")
    axes_names = [a.get("name") for a in sol.get("axes", [])]
    assert "auto_axis_1" in axes_names
    assert "auto_axis_2" in axes_names


def test_run_verify_legacy_when_auto_rubric_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_rubric=False + rubric_auto.json present -> ignored, legacy path used."""
    td = _seed(tmp_path, tid="wv3")
    (td.workspace_path() / "solution.py").write_text("def gcd(a,b): return a\n", encoding="utf-8")
    td.write_artifact("rubric_auto.json", {
        "axes": {
            "ignored": {"evaluator": "ast_grep", "weight": 1.0, "rule": "gcd in"},
        }
    })

    legacy_payload = {
        "axes": {"correctness": 0.7, "performance": 0.6},
        "weighted_score": 0.65,
        "evidence": "legacy path used",
        "issues": [],
    }
    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: _fake_completion(json.dumps(legacy_payload)),
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    cfg = Config()
    cfg.runtime.auto_rubric = False
    workers.run_verify(td, cfg)

    sol = td.read_artifact("solution.json")
    # legacy schema: axes is a *dict*, not a list
    assert isinstance(sol, dict)
    assert sol.get("evidence") == "legacy path used"
