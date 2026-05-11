"""v0.14 — Research multi-searcher + consolidator tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_loop.config import Config, Runtime, ResearcherSpec
from agent_loop.state import TaskDir, new_task_id


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()
    td.task_md_path().write_text("dummy task", encoding="utf-8")
    return td


class _R:
    def __init__(self, text: str = "stub findings") -> None:
        self.text = text
        self.prompt_tokens = 1
        self.completion_tokens = 1
        self.cost_usd = 0.0
        self.latency_s = 0.0
        self.model = "(fake)"


def test_researchers_field_normalizes_strings() -> None:
    cfg = Config(runtime=Runtime(researchers=["claude/haiku-4-5", "claude/opus-4-7"]))
    assert isinstance(cfg.runtime.researchers, list)
    assert cfg.runtime.researchers[0].provider == "claude/haiku-4-5"
    assert cfg.runtime.researchers[1].provider == "claude/opus-4-7"


def test_researchers_empty_normalized_to_none() -> None:
    cfg = Config(runtime=Runtime(researchers=[]))
    assert cfg.runtime.researchers is None


def test_run_research_single_when_no_researchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward-compat: missing `researchers` keeps the legacy single-call R."""
    td = _td(tmp_path)
    seen: list[str] = []

    def fake(phase, prompt, *, system=None, config=None, workspace=None):
        seen.append(phase)
        return _R("single findings body")

    monkeypatch.setattr("agent_loop.workers.call_model", fake)
    from agent_loop.workers import run_research
    run_research(td, Config())  # no researchers
    # exactly one research call
    assert seen == ["research"]
    assert (td._artifacts_dir() / "findings.md").read_text(encoding="utf-8") == "single findings body"


def test_run_research_multi_writes_per_searcher_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _td(tmp_path)
    seen_providers: list[str] = []

    def fake(phase, prompt, *, system=None, config=None, workspace=None):
        # Each searcher should have a different provider set on the cfg.
        seen_providers.append(config.models.research)
        return _R(f"findings from {config.models.research}")

    monkeypatch.setattr("agent_loop.workers.call_model", fake)
    cfg = Config(runtime=Runtime(researchers=[
        ResearcherSpec(provider="claude/haiku-4-5", focus="code facts"),
        ResearcherSpec(provider="claude/opus-4-7", focus="external standards"),
    ]))
    from agent_loop.workers import run_research
    run_research(td, cfg)

    # 2 searchers + 1 consolidator = 3 calls
    assert len(seen_providers) == 3
    # First two are the per-searcher providers (order-independent)
    assert set(seen_providers[:2]) == {"claude/haiku-4-5", "claude/opus-4-7"}
    # Per-searcher artifacts exist
    assert (td._artifacts_dir() / "findings_1.md").exists()
    assert (td._artifacts_dir() / "findings_2.md").exists()
    # Canonical findings.md is the consolidator's output
    assert (td._artifacts_dir() / "findings.md").exists()


def test_run_research_multi_decision_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _td(tmp_path)
    monkeypatch.setattr(
        "agent_loop.workers.call_model",
        lambda *a, **kw: _R("body"),
    )
    cfg = Config(runtime=Runtime(researchers=["claude/haiku-4-5"]))
    from agent_loop.workers import run_research
    run_research(td, cfg)
    log = td.decision_log_path().read_text(encoding="utf-8")
    assert "research_searcher" in log
    assert "research_consolidator" in log
