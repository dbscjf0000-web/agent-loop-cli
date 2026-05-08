"""v0.12.0 follow-up — llm_rubric spec.file support tests.

Covers the missed-evaluator fix: before this patch, llm_rubric hardcoded
``solution.py`` so non-code task rubrics (manuscript, spec, ...) were
evaluated against an empty file and scored ~0.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_loop.config import Config
from agent_loop.evaluators import llm_rubric
from agent_loop.evaluators.llm_rubric import _build_prompt, run_llm_rubric
from agent_loop.state import TaskDir, new_task_id


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()
    return td


# ---------------------------------------------------------------------------
# prompt language adapts to the artifact kind
# ---------------------------------------------------------------------------
def test_prompt_says_code_for_default() -> None:
    p = _build_prompt("axis", "criterion", "source", "code")
    assert "Score the following code" in p
    assert "===== code =====" in p


def test_prompt_says_document_for_markdown() -> None:
    p = _build_prompt("axis", "criterion", "source", "document")
    assert "Score the following document" in p
    assert "===== document =====" in p


# ---------------------------------------------------------------------------
# end-to-end: a stubbed call_model returns a fixed score so we can assert
# that the right file was loaded and shown to the prompt.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.latency_s = 0.0
        self.model = "(fake)"


def _stub_call_model(captured: dict[str, Any]):
    def fake(phase, prompt, *, system=None, config=None, workspace=None):
        captured["prompt"] = prompt
        return _FakeResponse('{"score": 0.85, "evidence": "ok"}')
    return fake


def test_default_loads_solution_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = _td(tmp_path)
    (td.workspace_path() / "solution.py").write_text(
        "def f(): return 'hello'\n", encoding="utf-8"
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr("agent_loop.models.call_model", _stub_call_model(captured))

    score = run_llm_rubric(
        name="quality",
        spec={"weight": 1.0, "criterion": "is it good"},
        task_dir=td, config=Config(),
    )
    assert score.score == pytest.approx(0.85)
    assert "def f()" in captured["prompt"]
    assert "===== code =====" in captured["prompt"]


def test_spec_file_loads_manuscript_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug: before fix, this would always read solution.py and the
    manuscript content would never reach the LLM."""
    td = _td(tmp_path)
    (td.workspace_path() / "manuscript.md").write_text(
        "# Title\n\n## Abstract\nfoo bar\n", encoding="utf-8"
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr("agent_loop.models.call_model", _stub_call_model(captured))

    score = run_llm_rubric(
        name="writing_quality",
        spec={"weight": 1.0, "criterion": "is the prose good", "file": "manuscript.md"},
        task_dir=td, config=Config(),
    )
    assert score.score == pytest.approx(0.85)
    assert "# Title" in captured["prompt"]
    assert "## Abstract" in captured["prompt"]
    assert "===== document =====" in captured["prompt"]


def test_spec_file_unsafe_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = _td(tmp_path)
    monkeypatch.setattr("agent_loop.models.call_model", _stub_call_model({}))

    score = run_llm_rubric(
        name="x",
        spec={"weight": 1.0, "criterion": "x", "file": "../../etc/passwd"},
        task_dir=td, config=Config(),
    )
    assert score.score == 0.0
    assert "unsafe" in score.evidence


def test_missing_file_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = _td(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("agent_loop.models.call_model", _stub_call_model(captured))

    score = run_llm_rubric(
        name="q",
        spec={"weight": 1.0, "criterion": "x", "file": "missing.md"},
        task_dir=td, config=Config(),
    )
    # Should still call the model, with the placeholder text in the prompt.
    assert "(no missing.md)" in captured["prompt"]
    assert score.score == pytest.approx(0.85)
