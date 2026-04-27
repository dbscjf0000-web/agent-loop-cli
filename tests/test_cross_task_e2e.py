"""End-to-end mocked test: orchestrator commits to global memory at run end,
and a subsequent task's snapshot picks up the previously committed patterns."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config
from agent_loop.context import ContextEngine
from agent_loop.orchestrator import Orchestrator
from agent_loop.state import TaskDir


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
    )


def _patch_litellm(monkeypatch: pytest.MonkeyPatch, canned: dict[str, str], cfg: Config) -> None:
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


def test_orchestrator_commits_to_global_at_run_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One mocked cycle: at run end, patterns.md + task_index.jsonl are written."""
    canned = {
        "research": "# Findings\n- ok",
        "plan": "# Plan\n1. write add()",
        "implement": "## notes\n```python\ndef add(a,b):\n    return a+b\n```\n",
        "verify": json.dumps({
            "axes": {"correctness": 1.0},
            "weighted_score": 0.97,
            "evidence": "adds correctly",
            "issues": [],
        }),
        # First-cycle judge skip (no best_solution.json yet).
        "judge": json.dumps({"better": True, "action": "stop", "scores": {}}),
    }
    cfg = Config()
    # Override global root via config (not env), to a tmp dir.
    global_dir = tmp_path / "global-e2e"
    cfg.runtime.cross_task_memory_dir = str(global_dir)
    _patch_litellm(monkeypatch, canned, cfg)

    td = TaskDir(root=tmp_path / "tasks", task_id="e2e-1")
    # Pre-seed core_facts.md so commit_to_global has something to harvest.
    # The judge phase will append history; CORE: lines come from compacted hints.
    # For this e2e, write directly to core_facts.md via ContextEngine.
    eng = ContextEngine(
        td, global_root=global_dir, cross_task=True
    )
    td.init()
    eng.init()
    (td.memory_dir() / "core_facts.md").write_text(
        "CORE: e2e-test pattern alpha\n", encoding="utf-8"
    )

    orch = Orchestrator(td, cfg)
    result = orch.run("Implement add(a,b).", max_cycles=2, mode="auto", max_redo=1)
    assert result["final_status"] == "stop"

    # patterns.md must contain our seed line.
    p = global_dir / "patterns.md"
    assert p.exists()
    body = p.read_text(encoding="utf-8")
    assert "CORE: e2e-test pattern alpha" in body

    # task_index.jsonl must have one row for this task.
    idx = global_dir / "task_index.jsonl"
    assert idx.exists()
    rows = [
        json.loads(ln)
        for ln in idx.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "e2e-1"
    assert rows[0]["final_status"] == "stop"
    assert rows[0]["weighted_score"] is not None


def test_second_task_sees_first_task_global_patterns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run task A end-to-end (mocked), then task B's snapshot must see A's committed patterns."""
    canned = {
        "research": "# Findings",
        "plan": "# Plan",
        "implement": "```python\ndef f():\n    return 1\n```\n",
        "verify": json.dumps({"axes": {"correctness": 1.0}, "weighted_score": 0.96, "evidence": "ok", "issues": []}),
        "judge": json.dumps({"better": True, "action": "stop", "scores": {}}),
    }
    cfg = Config()
    global_dir = tmp_path / "global-shared"
    cfg.runtime.cross_task_memory_dir = str(global_dir)
    _patch_litellm(monkeypatch, canned, cfg)

    # Task A
    td_a = TaskDir(root=tmp_path / "tasks", task_id="task-A")
    td_a.init()
    eng_a = ContextEngine(td_a, global_root=global_dir, cross_task=True)
    eng_a.init()
    (td_a.memory_dir() / "core_facts.md").write_text(
        "CORE: shared cross-task lesson\n", encoding="utf-8"
    )
    Orchestrator(td_a, cfg).run("Trivial A.", max_cycles=1, mode="auto", max_redo=1)

    # Task B — fresh task dir, same global root. Its snapshot should include A's pattern.
    td_b = TaskDir(root=tmp_path / "tasks", task_id="task-B")
    td_b.init()
    eng_b = ContextEngine(td_b, global_root=global_dir, cross_task=True)
    eng_b.init()
    snap_b = eng_b.snapshot()
    assert "CORE: shared cross-task lesson" in snap_b.global_patterns
    rendered = snap_b.render()
    assert "Global Patterns" in rendered
    assert "CORE: shared cross-task lesson" in rendered
