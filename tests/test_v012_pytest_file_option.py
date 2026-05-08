"""v0.12.0 — pytest_runner spec.file option tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop.config import Config
from agent_loop.evaluators.pytest_runner import _load_solution, run_pytest
from agent_loop.state import TaskDir, new_task_id


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()
    return td


def test_load_solution_default_path(tmp_path: Path) -> None:
    td = _td(tmp_path)
    (td.workspace_path() / "solution.py").write_text(
        "def add(a, b): return a + b\n", encoding="utf-8"
    )
    mod = _load_solution(td)  # default file_name="solution.py"
    assert mod.add(2, 3) == 5


def test_load_solution_custom_file_name(tmp_path: Path) -> None:
    td = _td(tmp_path)
    (td.workspace_path() / "smart_sort.py").write_text(
        "def smart_sort(xs): return sorted(xs)\n", encoding="utf-8"
    )
    mod = _load_solution(td, "smart_sort.py")
    assert mod.smart_sort([3, 1, 2]) == [1, 2, 3]


def test_load_solution_traversal_rejected(tmp_path: Path) -> None:
    td = _td(tmp_path)
    with pytest.raises(ValueError):
        _load_solution(td, "../passwd")
    with pytest.raises(ValueError):
        _load_solution(td, "dir/file.py")


def test_load_solution_missing_file(tmp_path: Path) -> None:
    td = _td(tmp_path)
    with pytest.raises(FileNotFoundError):
        _load_solution(td, "nonexistent.py")


def test_run_pytest_with_spec_file(tmp_path: Path) -> None:
    td = _td(tmp_path)
    (td.workspace_path() / "smart_sort.py").write_text(
        "def smart_sort(xs): return sorted(xs)\n", encoding="utf-8"
    )
    spec = {
        "weight": 1.0,
        "file": "smart_sort.py",
        "test": "assert smart_sort([3,1,2]) == [1,2,3]",
    }
    score = run_pytest(name="correctness", spec=spec, task_dir=td, config=Config())
    assert score.score == 1.0
    assert score.evaluator == "pytest"


def test_run_pytest_default_file_unchanged(tmp_path: Path) -> None:
    """Backward compat: rubric without ``spec["file"]`` still loads solution.py."""
    td = _td(tmp_path)
    (td.workspace_path() / "solution.py").write_text(
        "def f(x): return x * 2\n", encoding="utf-8"
    )
    spec = {
        "weight": 1.0,
        "test": "assert f(3) == 6",
    }
    score = run_pytest(name="correctness", spec=spec, task_dir=td, config=Config())
    assert score.score == 1.0
