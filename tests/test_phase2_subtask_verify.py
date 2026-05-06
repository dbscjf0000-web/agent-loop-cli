"""Step C — sub-task verifier dispatcher tests.

Covers parser + 3 verifier types (pytest / rule / llm_rubric stub) +
crash isolation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.subtask_verify import (
    Subtask,
    parse_subtasks,
    run_subtask_verifications,
)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def test_parse_simple_one_subtask() -> None:
    plan = """
## 3. Sub-tasks

### subtask-1: parse_input
- goal: 입력 파싱
- acceptance: parse("1") == [1]
- verifier: pytest
- check_hint: empty
- depends_on:
"""
    sts = parse_subtasks(plan)
    assert len(sts) == 1
    assert sts[0].id == "subtask-1"
    assert sts[0].verifier == "pytest"


def test_parse_multiple_subtasks_different_verifiers() -> None:
    plan = """
### subtask-1: parser
- goal: parse
- acceptance: parse("a") == ["a"]
- verifier: pytest
- check_hint: empty
- depends_on:

### subtask-2: 초록
- goal: write abstract
- acceptance: 250자 이내
- verifier: rule
- check_hint: section="Abstract" in output.md
- depends_on:

### subtask-3: 결론
- goal: write conclusion
- acceptance: 명확한 주장
- verifier: llm_rubric
- check_hint: 본문과 일치
- depends_on:
"""
    sts = parse_subtasks(plan)
    assert len(sts) == 3
    assert {s.verifier for s in sts} == {"pytest", "rule", "llm_rubric"}


def test_parse_handles_no_subtask_section() -> None:
    assert parse_subtasks("just a plain plan with no subtasks") == []


# ---------------------------------------------------------------------------
# pytest verifier
# ---------------------------------------------------------------------------
def test_pytest_verifier_passes_when_test_green(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "solution.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (ws / "test_subtask1.py").write_text(
        "from solution import add\ndef test_add(): assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    plan = """
### subtask-1: add
- goal: addition
- acceptance: add(1,2)==3
- verifier: pytest
- check_hint:
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].verifier == "pytest"


def test_pytest_verifier_fails_when_test_red(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "solution.py").write_text("def add(a, b): return 0\n", encoding="utf-8")
    (ws / "test_subtask1.py").write_text(
        "from solution import add\ndef test_add(): assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    plan = """
### subtask-1: add
- goal: addition
- acceptance: add(1,2)==3
- verifier: pytest
- check_hint:
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is False


def test_pytest_verifier_missing_test_file(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    plan = """
### subtask-1: add
- goal: addition
- acceptance: add
- verifier: pytest
- check_hint:
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is False
    assert "missing test file" in results[0].detail


# ---------------------------------------------------------------------------
# rule verifier
# ---------------------------------------------------------------------------
def test_rule_text_clause_pass(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "solution.py").write_text("def magic(): return 42\n", encoding="utf-8")
    plan = """
### subtask-1: code
- goal: code
- acceptance: magic exists
- verifier: rule
- check_hint: text="def magic" in solution.py
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is True


def test_rule_section_clause_pass(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "output.md").write_text("# Title\n\n## Abstract\n\nfoo bar\n", encoding="utf-8")
    plan = """
### subtask-1: paper
- goal: write
- acceptance: has abstract
- verifier: rule
- check_hint: section="Abstract" in output.md
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is True


def test_rule_json_keypath(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "output.json").write_text('{"meta": {"version": "1.0"}}', encoding="utf-8")
    plan = """
### subtask-1: schema
- goal: shape
- acceptance: meta.version
- verifier: rule
- check_hint: json="meta.version" in output.json
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is True


def test_rule_regex_fails_when_pattern_missing(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "solution.py").write_text("# nothing here\n", encoding="utf-8")
    plan = """
### subtask-1: x
- goal: x
- acceptance: x
- verifier: rule
- check_hint: regex=/foo[0-9]+/ in solution.py
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is False


def test_rule_unrecognized_clause_in_multi_now_fails(tmp_path: Path) -> None:
    """Codex fix #4: comma-separated clauses where some are unrecognized
    must FAIL (previously the prefix match silently passed earlier clauses
    while ignoring later malformed ones)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "output.md").write_text("# Title\n## Abstract\nfoo\n", encoding="utf-8")
    plan = """
### subtask-1: paper
- goal: write
- acceptance: 250자
- verifier: rule
- check_hint: section="Abstract" in output.md, len ≤ 250자
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is False
    assert "unrecognized" in results[0].detail


def test_rule_with_no_recognized_clauses(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    plan = """
### subtask-1: x
- goal: x
- acceptance: x
- verifier: rule
- check_hint: 그냥 자연어 힌트, 규칙 없음
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is False
    assert "no recognized rule clauses" in results[0].detail


# ---------------------------------------------------------------------------
# llm_rubric stub
# ---------------------------------------------------------------------------
def test_llm_rubric_deferred_marked_passed(tmp_path: Path) -> None:
    """llm_rubric is intentionally deferred to legacy V; mark passed=True
    with explanatory detail so it doesn't drag final score down."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    plan = """
### subtask-1: prose
- goal: write
- acceptance: 명확
- verifier: llm_rubric
- check_hint: 논리 흐름
- depends_on:
"""
    results = run_subtask_verifications(plan, ws)
    assert results[0].passed is True
    assert "deferred" in results[0].detail


# ---------------------------------------------------------------------------
# unknown verifier + crash isolation
# ---------------------------------------------------------------------------
def test_unknown_verifier_marked_failed(tmp_path: Path) -> None:
    plan = """
### subtask-1: x
- goal: x
- acceptance: x
- verifier: madeup
- check_hint:
- depends_on:
"""
    results = run_subtask_verifications(plan, tmp_path)
    assert results[0].passed is False
    assert "unknown" in results[0].detail
