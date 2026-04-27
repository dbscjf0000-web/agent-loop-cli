"""End-to-end mocked test: orchestrator drives 1 cycle and the v0.2 ContextEngine
records history + sensors without any real LLM call."""
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


def test_orchestrator_populates_context_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One cycle (auto stop on first cycle) must:
       - create memory/ layout (history.jsonl + episodic.md + core_facts.md)
       - append a history record per phase
       - record `_cycle_quality` row in metrics.jsonl
    """
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
            "axes": {"correctness": 1.0},
            "weighted_score": 0.97,
            "evidence": "adds correctly",
            "issues": [],
        }),
        # judge will be skipped on cycle 1 (no best yet), so this is unused.
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

    td = TaskDir(root=tmp_path, task_id="ctx-e2e")
    orch = Orchestrator(td, cfg)
    result = orch.run("Implement add(a,b).", max_cycles=2, mode="auto", max_redo=1)
    assert result["final_status"] == "stop"

    # --- ContextEngine artifacts are present ---
    md = td.memory_dir()
    assert md.is_dir()
    history_lines = [
        ln for ln in (md / "history.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    # 4 phases ran (judge short-circuited but still appends history once).
    assert len(history_lines) >= 5
    phases_seen = {json.loads(ln)["phase"] for ln in history_lines}
    assert {"research", "plan", "implement", "verify", "judge"} <= phases_seen

    episodic = (md / "episodic.md").read_text(encoding="utf-8")
    assert "research" in episodic and "verify" in episodic

    # --- metrics.jsonl carries a `_cycle_quality` row with sensor keys ---
    metrics_path = td.path / "telemetry" / "metrics.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    quality_rows = [r for r in rows if r.get("phase") == "_cycle_quality"]
    assert quality_rows, "expected at least one `_cycle_quality` metrics row"
    q = quality_rows[-1]["quality"]
    for key in ("duplicate_ratio", "contradiction_count", "staleness_age_cycles", "relevance_score"):
        assert key in q


def test_resume_with_v0_1_memory_txt_migrates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A task dir created in v0.1 (only memory.txt) survives an orchestrator launch."""
    td = TaskDir(root=tmp_path, task_id="ctx-resume-legacy")
    td.init()  # creates layout including the (now-empty) memory.txt
    td.memory_md_path().write_text("v0.1 hint: prefer iterative\n", encoding="utf-8")

    # Stub out litellm so the run does no real work; we only need init paths.
    canned = {
        "research": "# Findings",
        "plan": "# Plan",
        "implement": "```python\ndef f(): pass\n```\n",
        "verify": json.dumps({"axes": {}, "weighted_score": 0.99, "evidence": "ok", "issues": []}),
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
    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: _resp(canned[phase_by_model[kw["model"]]]),
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    Orchestrator(td, cfg).run("trivial", max_cycles=1, mode="auto", max_redo=1)

    cf = (td.memory_dir() / "core_facts.md").read_text(encoding="utf-8")
    assert "prefer iterative" in cf
    bak = td.path / "memory.txt.v0_1.bak"
    assert bak.exists()
