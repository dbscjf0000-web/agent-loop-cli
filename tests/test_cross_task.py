"""Unit tests for v0.4 cross-task memory (ContextEngine extension)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.context import ContextEngine, MemorySnapshot
from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_engine(tmp_path: Path, *, name: str, cross_task: bool = True, max_chars: int = 4000) -> tuple[TaskDir, ContextEngine, Path]:
    td = TaskDir(root=tmp_path / "tasks", task_id=name)
    td.init()
    global_root = tmp_path / "global"
    eng = ContextEngine(
        td,
        global_root=global_root,
        cross_task=cross_task,
        global_max_chars=max_chars,
    )
    eng.init()
    return td, eng, global_root


# ---------------------------------------------------------------------------
# 1. global dir auto-creation
# ---------------------------------------------------------------------------

def test_global_dir_created_on_first_commit(tmp_path: Path) -> None:
    """The global directory is auto-created the first time commit_to_global runs."""
    td, eng, gdir = _make_engine(tmp_path, name="t1")
    assert not gdir.exists()  # not created yet
    # Seed a CORE: line in this task's core_facts.md.
    (td.memory_dir() / "core_facts.md").write_text(
        "CORE: prefer iterative over recursive for N>20\n", encoding="utf-8"
    )
    stat = eng.commit_to_global({"task_id": "t1"})
    assert stat["committed"] is True
    assert gdir.is_dir()
    assert (gdir / "patterns.md").exists()
    assert (gdir / "task_index.jsonl").exists()


# ---------------------------------------------------------------------------
# 2. patterns.md dedup append
# ---------------------------------------------------------------------------

def test_patterns_dedup_append(tmp_path: Path) -> None:
    """Same CORE: line committed by two tasks lands in patterns.md exactly once."""
    # First task commits 2 lines.
    td1, eng1, gdir = _make_engine(tmp_path, name="alpha")
    (td1.memory_dir() / "core_facts.md").write_text(
        "CORE: line A\nCORE: line B\n", encoding="utf-8"
    )
    s1 = eng1.commit_to_global({"task_id": "alpha"})
    assert s1["patterns_added"] == 2

    # Second task commits 1 duplicate + 1 new.
    td2 = TaskDir(root=tmp_path / "tasks", task_id="beta")
    td2.init()
    eng2 = ContextEngine(td2, global_root=gdir, cross_task=True)
    eng2.init()
    (td2.memory_dir() / "core_facts.md").write_text(
        "CORE: line A\nCORE: line C\n", encoding="utf-8"
    )
    s2 = eng2.commit_to_global({"task_id": "beta"})
    assert s2["patterns_added"] == 1  # only line C is new

    body = (gdir / "patterns.md").read_text(encoding="utf-8")
    # Each line appears exactly once.
    assert body.count("CORE: line A") == 1
    assert body.count("CORE: line B") == 1
    assert body.count("CORE: line C") == 1


# ---------------------------------------------------------------------------
# 3. task_index.jsonl one row per task (idempotent)
# ---------------------------------------------------------------------------

def test_task_index_one_row_per_task_id(tmp_path: Path) -> None:
    """Calling commit_to_global twice for the same task_id must not duplicate the index row."""
    td, eng, gdir = _make_engine(tmp_path, name="dup-task")
    summary = {
        "task_id": "dup-task",
        "weighted_score": 0.97,
        "cycles": 2,
        "task_md_first_line": "Implement add().",
        "final_status": "stop",
    }
    s1 = eng.commit_to_global(summary)
    s2 = eng.commit_to_global(summary)
    assert s1["index_added"] == 1
    assert s2["index_added"] == 0  # idempotent

    rows = [
        json.loads(ln)
        for ln in (gdir / "task_index.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "dup-task"
    assert rows[0]["weighted_score"] == 0.97


# ---------------------------------------------------------------------------
# 4. cross_task=False disables global I/O entirely
# ---------------------------------------------------------------------------

def test_cross_task_false_is_noop(tmp_path: Path) -> None:
    """When cross_task=False, commit + load both refuse to touch the global dir."""
    td, eng, gdir = _make_engine(tmp_path, name="off", cross_task=False)
    (td.memory_dir() / "core_facts.md").write_text(
        "CORE: should not propagate\n", encoding="utf-8"
    )
    stat = eng.commit_to_global({"task_id": "off"})
    assert stat["committed"] is False
    assert stat["reason"] == "disabled"
    assert not gdir.exists()  # no directory created

    # snapshot must not include global_patterns either.
    snap = eng.snapshot()
    assert snap.global_patterns == ""
    assert "Global Patterns" not in snap.render()


# ---------------------------------------------------------------------------
# 5. snapshot includes global_patterns
# ---------------------------------------------------------------------------

def test_snapshot_includes_global_patterns(tmp_path: Path) -> None:
    """snapshot() returns a slice of patterns.md when cross_task=True."""
    td, eng, gdir = _make_engine(tmp_path, name="snap")
    # Pre-seed the global patterns.md (simulating a prior task's commit).
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "patterns.md").write_text(
        "CORE: from prior task X\nCORE: from prior task Y\n", encoding="utf-8"
    )
    snap = eng.snapshot()
    assert isinstance(snap, MemorySnapshot)
    assert "CORE: from prior task X" in snap.global_patterns
    assert "CORE: from prior task Y" in snap.global_patterns
    rendered = snap.render()
    assert "# Global Patterns (cross-task)" in rendered
    assert "CORE: from prior task X" in rendered


# ---------------------------------------------------------------------------
# 6. max_chars truncates oldest lines
# ---------------------------------------------------------------------------

def test_max_chars_keeps_recent_truncates_old(tmp_path: Path) -> None:
    """When patterns.md exceeds max_chars, snapshot keeps the most recent suffix."""
    td, eng, gdir = _make_engine(tmp_path, name="trunc", max_chars=200)
    gdir.mkdir(parents=True, exist_ok=True)
    # Construct a file whose total size is well above 200.
    lines = [f"CORE: pattern number {i:03d} with extra padding text here" for i in range(30)]
    (gdir / "patterns.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    snap = eng.snapshot()
    gp = snap.global_patterns
    # Slice should be at most ~max_chars + small slack (we snap to newline).
    assert 0 < len(gp) <= 201
    # The last line must survive.
    assert "pattern number 029" in gp
    # The first line must be cut off.
    assert "pattern number 000" not in gp


# ---------------------------------------------------------------------------
# 7. commit_to_global is idempotent on consecutive calls
# ---------------------------------------------------------------------------

def test_commit_is_idempotent_for_same_task(tmp_path: Path) -> None:
    """Two calls with the same task_id + same core_facts.md must produce identical files."""
    td, eng, gdir = _make_engine(tmp_path, name="idemp")
    (td.memory_dir() / "core_facts.md").write_text(
        "CORE: idempotent line one\nCORE: idempotent line two\n", encoding="utf-8"
    )
    summary = {"task_id": "idemp", "weighted_score": 0.88, "cycles": 1, "task_md_first_line": "tt", "final_status": "stop"}

    s1 = eng.commit_to_global(summary)
    p1_text = (gdir / "patterns.md").read_text(encoding="utf-8")
    i1_text = (gdir / "task_index.jsonl").read_text(encoding="utf-8")

    s2 = eng.commit_to_global(summary)
    p2_text = (gdir / "patterns.md").read_text(encoding="utf-8")
    i2_text = (gdir / "task_index.jsonl").read_text(encoding="utf-8")

    assert s1["patterns_added"] == 2 and s2["patterns_added"] == 0
    assert s1["index_added"] == 1 and s2["index_added"] == 0
    assert p1_text == p2_text
    assert i1_text == i2_text


# ---------------------------------------------------------------------------
# 8. only CORE: lines are extracted (ignores noise)
# ---------------------------------------------------------------------------

def test_only_core_lines_extracted(tmp_path: Path) -> None:
    """Lines without a CORE: prefix in core_facts.md must NOT propagate to global."""
    td, eng, gdir = _make_engine(tmp_path, name="filt")
    (td.memory_dir() / "core_facts.md").write_text(
        "# Core Facts (heading)\n"
        "CORE: real pattern\n"
        "this is not a core line\n"
        "  CORE: indented also counts\n"
        "noise line\n",
        encoding="utf-8",
    )
    eng.commit_to_global({"task_id": "filt"})
    p = (gdir / "patterns.md").read_text(encoding="utf-8")
    assert "CORE: real pattern" in p
    assert "CORE: indented also counts" in p
    assert "this is not a core line" not in p
    assert "noise line" not in p
    assert "# Core Facts" not in p


# ---------------------------------------------------------------------------
# 9. _load_global_patterns honors cross_task=False
# ---------------------------------------------------------------------------

def test_load_returns_empty_when_disabled(tmp_path: Path) -> None:
    """_load_global_patterns returns '' when the engine is disabled, even if file exists."""
    td, eng_off, gdir = _make_engine(tmp_path, name="loadoff", cross_task=False)
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "patterns.md").write_text("CORE: should be ignored\n", encoding="utf-8")
    assert eng_off._load_global_patterns() == ""

    # Same dir, fresh engine with cross_task=True must see it.
    td2 = TaskDir(root=tmp_path / "tasks", task_id="loadon")
    td2.init()
    eng_on = ContextEngine(td2, global_root=gdir, cross_task=True)
    eng_on.init()
    assert "CORE: should be ignored" in eng_on._load_global_patterns()


# ---------------------------------------------------------------------------
# 10. snapshot when global file is missing returns empty global_patterns
# ---------------------------------------------------------------------------

def test_snapshot_no_global_file(tmp_path: Path) -> None:
    """snapshot() must not error when ~/.agent-loop/global/patterns.md is absent."""
    td, eng, gdir = _make_engine(tmp_path, name="nopath")
    assert not gdir.exists()
    snap = eng.snapshot()
    assert snap.global_patterns == ""
    rendered = snap.render()
    assert "Global Patterns" not in rendered  # section omitted


# ---------------------------------------------------------------------------
# 11. backward compat — ContextEngine() with no kwargs still works (default cross_task=True)
# ---------------------------------------------------------------------------

def test_default_constructor_is_backward_compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ContextEngine(td) with no kwargs must still work — uses default global root.

    We monkeypatch HOME so the test never touches the real ~/.agent-loop/.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    td = TaskDir(root=tmp_path / "tasks", task_id="bc")
    td.init()
    eng = ContextEngine(td)  # all defaults
    eng.init()

    # snapshot() must not raise. global_patterns is empty (no prior tasks).
    snap = eng.snapshot()
    assert snap.global_patterns == ""

    # commit with a CORE: line — directory should be created under fake_home.
    (td.memory_dir() / "core_facts.md").write_text(
        "CORE: bc test line\n", encoding="utf-8"
    )
    stat = eng.commit_to_global({"task_id": "bc"})
    assert stat["committed"] is True
    expected_dir = fake_home / ".agent-loop" / "global"
    assert expected_dir.is_dir()
    assert "CORE: bc test line" in (expected_dir / "patterns.md").read_text(encoding="utf-8")
