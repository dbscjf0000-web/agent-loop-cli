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


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
    )


def test_orchestrator_one_cycle_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Single cycle: judge says stop on first cycle (auto better=true short-circuit)."""

    # Construct phase-aware fake responses.
    canned = {
        "research": "# Findings\n- ok",
        "plan": "# Plan\n1. write add()",
        "implement": (
            "## notes\nadd\n\n"
            "```python\n"
            "def add(a, b):\n    return a + b\n"
            "```\n"
        ),
        "verify": json.dumps({
            "axes": {"correctness": 1.0, "performance": 1.0},
            "weighted_score": 0.97,
            "evidence": "adds correctly",
            "issues": [],
        }),
        # judge will be skipped on cycle 1 (no best yet) so this is unused
        "judge": json.dumps({"better": True, "action": "stop", "scores": {}}),
    }

    cfg = Config()
    phase_by_model = {
        cfg.models.research: "research",
        cfg.models.plan: "plan",
        cfg.models.implement: "implement",
        cfg.models.verify: "verify",
        cfg.models.judge: "judge",
    }

    def fake_completion(**kw: Any) -> SimpleNamespace:
        phase = phase_by_model[kw["model"]]
        return _resp(canned[phase])

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    td = TaskDir(root=tmp_path, task_id="orchA")
    orch = Orchestrator(td, cfg)
    result = orch.run("Implement add(a,b).", max_cycles=3, mode="auto", max_redo=2)

    assert result["final_status"] == "stop"
    assert result["cycles_run"] == 1
    # Best solution promoted.
    assert td.has_artifact("best_solution.json")
    # solution.py persisted in workspace.
    assert (td.workspace_path() / "solution.py").exists()
    # Telemetry rows present (>=4 phases for cycle 1; judge call was skipped).
    metrics_path = td.path / "telemetry" / "metrics.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    phases_seen = {r["phase"] for r in rows}
    assert {"research", "plan", "implement", "verify"} <= phases_seen


def test_orchestrator_max_redo_break(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force judge to keep returning better=False; orchestrator must stop on max_redo."""

    judge_text = json.dumps({
        "better": False,
        "action": "redo_R",
        "reason": "regressed",
        "hint": "try again",
        "scores": {"this_cycle": 0.1, "best": 0.5, "delta": -0.4},
    })
    verify_text = json.dumps({
        "axes": {"correctness": 0.1},
        "weighted_score": 0.1,
        "evidence": "broken",
        "issues": ["nope"],
    })

    cfg = Config()
    phase_by_model = {
        cfg.models.research: "# Findings",
        cfg.models.plan: "# Plan",
        cfg.models.implement: "```python\ndef f():\n    pass\n```\n",
        cfg.models.verify: verify_text,
        cfg.models.judge: judge_text,
    }

    def fake_completion(**kw: Any) -> SimpleNamespace:
        return _resp(phase_by_model[kw["model"]])

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    td = TaskDir(root=tmp_path, task_id="orchB")
    # Pre-seed a best_solution so the judge LLM actually runs (not first-cycle skip).
    td.init()
    td.write_artifact("best_solution.json", {"weighted_score": 0.5})
    (td.workspace_path() / "best_solution.py").write_text("def f(): pass\n", encoding="utf-8")

    orch = Orchestrator(td, cfg)
    result = orch.run("Trivial.", max_cycles=10, mode="auto", max_redo=2)

    assert result["final_status"] == "max_redo"
    assert result["cycles_run"] >= 2
