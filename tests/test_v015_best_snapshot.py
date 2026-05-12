"""v0.15 — workspace/best/ multi-file snapshot + rollback tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.config import Config
from agent_loop.orchestrator import Orchestrator
from agent_loop.state import TaskDir, new_task_id


def _td(tmp_path: Path) -> tuple[TaskDir, Orchestrator]:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()
    td.write_artifact(
        "solution.json",
        {"weighted_score": 0.949, "summary": "ok", "axes": []},
    )
    orch = Orchestrator(td, Config())
    return td, orch


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------
def test_promote_copies_all_workspace_files_to_best(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    (ws / "manuscript.md").write_text("# manuscript v2\n", encoding="utf-8")
    (ws / "SI.md").write_text("# SI v2\n", encoding="utf-8")
    (ws / "refs.json").write_text('{"a":1}', encoding="utf-8")

    orch._promote_to_best()

    best = ws / "best"
    assert best.is_dir()
    assert (best / "manuscript.md").read_text(encoding="utf-8") == "# manuscript v2\n"
    assert (best / "SI.md").read_text(encoding="utf-8") == "# SI v2\n"
    assert (best / "refs.json").read_text(encoding="utf-8") == '{"a":1}'
    # legacy single-file behavior preserved when solution.py exists


def test_promote_writes_manifest(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    (td.workspace_path() / "manuscript.md").write_text("body", encoding="utf-8")

    orch._promote_to_best()

    mf = td.workspace_path() / "best" / "best_manifest.json"
    assert mf.exists()
    m = json.loads(mf.read_text(encoding="utf-8"))
    assert m["score"] == 0.949
    assert any(f["name"] == "manuscript.md" for f in m["files"])
    assert m["files"][0]["size"] == 4
    assert len(m["files"][0]["sha256"]) == 64


def test_promote_excludes_pycache_and_symlinks(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    (ws / "code.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
    target = tmp_path / "host"
    target.mkdir()
    (ws / "leak").symlink_to(target)

    orch._promote_to_best()
    best = ws / "best"
    assert (best / "code.py").exists()
    assert not (best / "__pycache__").exists()
    assert not (best / "leak").exists()


def test_promote_atomic_swap_replaces_previous_best(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    (ws / "a.md").write_text("first", encoding="utf-8")
    orch._promote_to_best()
    assert (ws / "best" / "a.md").read_text(encoding="utf-8") == "first"

    # Second promote with different content.
    (ws / "a.md").write_text("second", encoding="utf-8")
    orch._promote_to_best()
    assert (ws / "best" / "a.md").read_text(encoding="utf-8") == "second"
    # tmp dir should not linger
    assert not (ws / ".best.tmp").exists()


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------
def test_rollback_restores_multi_file_state(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    # cycle 2 best
    (ws / "manuscript.md").write_text("good v2", encoding="utf-8")
    (ws / "SI.md").write_text("good SI v2", encoding="utf-8")
    orch._promote_to_best()
    td.write_artifact(
        "best_solution.json",
        {"weighted_score": 0.949, "summary": "best", "axes": []},
    )

    # cycle 3 regress
    (ws / "manuscript.md").write_text("bad v3", encoding="utf-8")
    (ws / "SI.md").unlink()  # cycle 3 also dropped SI
    (ws / "stale.md").write_text("garbage", encoding="utf-8")

    orch._rollback_to_best()

    assert (ws / "manuscript.md").read_text(encoding="utf-8") == "good v2"
    assert (ws / "SI.md").read_text(encoding="utf-8") == "good SI v2"
    assert not (ws / "stale.md").exists()  # rolled back
    assert (ws / "best").is_dir()  # best/ preserved across rollback


def test_rollback_legacy_solution_py_when_best_dir_absent(tmp_path: Path) -> None:
    """Backward compat: tasks that ran before v0.15 only have
    ``best_solution.py`` — rollback must still restore it."""
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    (ws / "best_solution.py").write_text("def best(): pass\n", encoding="utf-8")
    td.write_artifact(
        "best_solution.json",
        {"weighted_score": 0.9, "axes": []},
    )
    (ws / "solution.py").write_text("def regressed(): pass\n", encoding="utf-8")

    # No best/ dir — should fall through to legacy single-file path.
    orch._rollback_to_best()
    assert (ws / "solution.py").read_text(encoding="utf-8") == "def best(): pass\n"


def test_promote_then_rollback_round_trip(tmp_path: Path) -> None:
    td, orch = _td(tmp_path)
    ws = td.workspace_path()
    (ws / "x.md").write_text("v1", encoding="utf-8")
    orch._promote_to_best()
    td.write_artifact(
        "best_solution.json",
        {"weighted_score": 0.9, "axes": []},
    )

    (ws / "x.md").write_text("v2 bad", encoding="utf-8")
    (ws / "junk.txt").write_text("junk", encoding="utf-8")

    orch._rollback_to_best()
    assert (ws / "x.md").read_text(encoding="utf-8") == "v1"
    assert not (ws / "junk.txt").exists()
