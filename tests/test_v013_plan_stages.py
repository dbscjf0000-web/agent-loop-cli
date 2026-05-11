"""v0.13 — plan stage parser tests."""
from __future__ import annotations

from agent_loop.workers import _extract_plan_stages


PLAN_NO_STAGES = """\
# Plan
## 3. Sub-tasks
### subtask-1: parse
- goal: x
"""

PLAN_TWO_STAGES = """\
# Plan
## 3. Sub-tasks

### stage 1 (병렬)
- subtask-1: 약어 expand
  - goal: ...
  - model: claude/opus-4-7
- subtask-4: refs sort
  - goal: ...
  - model: claude/haiku-4-5

### stage 2 (앞 stage 완료 후)
- subtask-2: 톤 polish
  - depends_on: subtask-1
  - model: claude/opus-4-7

## 4. 검증 계획
- ...
"""

PLAN_STAGES_NO_MODEL = """\
### stage 1
- subtask-1: foo
  - goal: bar

### stage 2
- subtask-2: baz
  - goal: qux
"""


def test_no_stages_returns_empty() -> None:
    assert _extract_plan_stages(PLAN_NO_STAGES) == []


def test_two_stages_with_models() -> None:
    stages = _extract_plan_stages(PLAN_TWO_STAGES)
    assert len(stages) == 2
    assert stages[0].index == 1
    assert {s.id for s in stages[0].subtasks} == {"subtask-1", "subtask-4"}
    assert stages[0].subtasks[0].model in {"claude/opus-4-7", "claude/haiku-4-5"}
    assert stages[1].index == 2
    assert stages[1].subtasks[0].id == "subtask-2"
    assert stages[1].subtasks[0].model == "claude/opus-4-7"


def test_stages_without_model_field() -> None:
    stages = _extract_plan_stages(PLAN_STAGES_NO_MODEL)
    assert len(stages) == 2
    for s in stages:
        for st in s.subtasks:
            assert st.model is None


def test_empty_plan() -> None:
    assert _extract_plan_stages("") == []


PLAN_HEADER_STYLE = """\
### stage 1 (병렬)
### subtask-1: 초기 notes.md 골격
- goal: ...
- model: claude/haiku-4-5

### subtask-2: TODO 보강
- depends_on: subtask-1
- model: claude/opus-4-7

### stage 2
### subtask-3: 검증
- goal: ...
"""


PLAN_LEVEL4_HEADERS = """\
### stage 1
#### subtask-1: foo
- model: claude/haiku-4-5

### stage 2
#### subtask-2: bar
#### subtask-3: baz
"""


def test_level4_subtask_headers_recognized() -> None:
    """LLMs often emit `#### subtask-N:` under `### stage N:` for clean
    visual nesting. Parser must accept any level >= 3."""
    stages = _extract_plan_stages(PLAN_LEVEL4_HEADERS)
    assert len(stages) == 2
    assert stages[0].subtasks[0].id == "subtask-1"
    assert {s.id for s in stages[1].subtasks} == {"subtask-2", "subtask-3"}


def test_header_style_subtasks_recognized() -> None:
    """Real-world catch: LLMs often emit `### subtask-N:` (inherited from
    the v0.12 plan template) instead of `- subtask-N:` bullets. Parser
    must accept both formats under a stage header."""
    stages = _extract_plan_stages(PLAN_HEADER_STYLE)
    assert len(stages) == 2
    assert {s.id for s in stages[0].subtasks} == {"subtask-1", "subtask-2"}
    assert stages[1].subtasks[0].id == "subtask-3"
