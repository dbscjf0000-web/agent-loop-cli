"""Mock e2e for run_verify with a rubric: no LLM, full disk roundtrip."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop import workers
from agent_loop.config import Config
from agent_loop.state import TaskDir


def test_run_verify_e2e_writes_axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="e2e1")
    td.init()
    td.task_md_path().write_text("# Task\nadd numbers", encoding="utf-8")
    (td.workspace_path() / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    td.write_artifact(
        "rubric.json",
        {
            "axes": {
                "correctness": {
                    "evaluator": "pytest",
                    "weight": 1.0,
                    "test": "assert add(2, 3) == 5",
                }
            }
        },
    )

    # If anything tries to call the LLM, blow up the test.
    def explode(**_):
        raise AssertionError("LLM call was made; rubric path should not need it")

    import agent_loop.models as models_mod

    monkeypatch.setattr(models_mod.litellm, "completion", explode)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    resp = workers.run_verify(td, Config())
    assert resp.cost_usd == 0.0
    assert resp.model == "(verify_engine: rubric)"

    sol = td.read_artifact("solution.json")
    assert isinstance(sol, dict)
    assert sol["weighted_score"] == 1.0
    assert isinstance(sol["axes"], list)
    assert sol["axes"][0]["name"] == "correctness"
    assert sol["axes"][0]["is_ground_truth"] is True

    # ContextEngine append happened (history.jsonl line written)
    history = (td.memory_dir() / "history.jsonl").read_text(encoding="utf-8")
    assert "verify" in history
    assert "weighted" not in history or '"phase": "verify"' in history
