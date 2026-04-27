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


# ---------- _extract_json ----------

def test_extract_json_direct() -> None:
    assert workers._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced() -> None:
    text = "Here you go:\n```json\n{\"axes\": {\"correctness\": 0.7}}\n```\n"
    assert workers._extract_json(text) == {"axes": {"correctness": 0.7}}


def test_extract_json_brace_slice() -> None:
    text = "preamble blah blah {\"better\": true, \"action\": \"stop\"} trailing"
    assert workers._extract_json(text) == {"better": True, "action": "stop"}


def test_extract_json_failure() -> None:
    with pytest.raises(ValueError):
        workers._extract_json("no braces here")


# ---------- _extract_python ----------

def test_extract_python_basic() -> None:
    text = "intro\n```python\ndef f(): return 1\n```\nafter"
    code, prose = workers._extract_python(text)
    assert "def f(): return 1" in code
    assert code.endswith("\n")
    assert "intro" in prose and "after" in prose


def test_extract_python_no_block() -> None:
    code, prose = workers._extract_python("just prose")
    assert code == ""
    assert prose == "just prose"


# ---------- run_judge first-cycle short-circuit ----------

def test_run_judge_first_cycle_no_llm_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="abc123")
    td.init()
    td.write_artifact("solution.json", {"weighted_score": 0.7, "axes": {"correctness": 0.7}})

    called = {"n": 0}

    def boom(**kwargs: Any) -> Any:
        called["n"] += 1
        return _fake_completion("should not be called")

    monkeypatch.setattr(models_mod.litellm, "completion", boom)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    resp = workers.run_judge(td, Config())
    assert called["n"] == 0
    j = td.read_artifact("judge_result.json")
    assert isinstance(j, dict)
    assert j["better"] is True
    assert resp.model.startswith("(skipped")


# ---------- run_judge with best_solution: LLM is called ----------

def test_run_judge_with_best_calls_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="def456")
    td.init()
    td.write_artifact("solution.json", {"weighted_score": 0.6, "axes": {"correctness": 0.6}})
    td.write_artifact("best_solution.json", {"weighted_score": 0.5})

    judge_payload = {
        "better": True,
        "action": "stop",
        "reason": "improved",
        "hint": "",
        "scores": {"this_cycle": 0.6, "best": 0.5, "delta": 0.1},
    }

    def fake(**kwargs: Any) -> Any:
        return _fake_completion(json.dumps(judge_payload))

    monkeypatch.setattr(models_mod.litellm, "completion", fake)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    workers.run_judge(td, Config())
    j = td.read_artifact("judge_result.json")
    assert isinstance(j, dict)
    assert j["action"] == "stop"
    assert j["scores"]["delta"] == pytest.approx(0.1)


# ---------- run_research persists findings.md ----------

def test_run_research_writes_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="r1")
    td.init()
    td.task_md_path().write_text("Implement foo()", encoding="utf-8")

    monkeypatch.setattr(
        models_mod.litellm,
        "completion",
        lambda **kw: _fake_completion("# Findings\n- foo is a function"),
    )
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    resp = workers.run_research(td, Config())
    assert "Findings" in td.read_artifact("findings.md")
    assert resp.text.startswith("# Findings")


# ---------- run_implement extracts code block ----------

def test_run_implement_writes_solution_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    td = TaskDir(root=tmp_path, task_id="i1")
    td.init()
    td.task_md_path().write_text("Implement add(a,b)", encoding="utf-8")
    td.write_artifact("plan.md", "# Plan\nImplement add()")

    response = (
        "## notes\nsimple add\n\n"
        "```python\n"
        "def add(a, b):\n    return a + b\n"
        "```\n"
        "## done\n"
    )

    monkeypatch.setattr(models_mod.litellm, "completion", lambda **kw: _fake_completion(response))
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    workers.run_implement(td, Config())
    sol = (td.workspace_path() / "solution.py").read_text(encoding="utf-8")
    assert "def add(a, b)" in sol
    log = td.read_artifact("execution_log.md")
    assert "notes" in log
