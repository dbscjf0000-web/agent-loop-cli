"""Unit tests for v0.2 ContextEngine — 3-tier memory + sensors + compactor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_loop.context import ContextEngine, MemorySnapshot
from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# init / migration
# ---------------------------------------------------------------------------

def test_init_creates_memory_layout(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-init")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    md = td.memory_dir()
    assert md.is_dir()
    assert (md / "history.jsonl").exists()
    assert (md / "episodic.md").exists()
    assert (md / "core_facts.md").exists()


def test_init_is_idempotent(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-idem")
    td.init()
    eng = ContextEngine(td)
    eng.init()
    eng.append_history({"cycle": 1, "phase": "research", "summary": "hi"})

    # Calling init again must NOT clobber history or pre-existing files.
    eng.init()
    rows = (td.memory_dir() / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["summary"] == "hi"


def test_v0_1_memory_txt_migrates_into_core_facts(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-migrate")
    td.init()
    # Simulate a v0.1 task with hand-written memory.txt content.
    legacy = "Earlier learning: prefer iterative algorithms for N>20\nCORE: avoid recursion deep"
    td.memory_md_path().write_text(legacy, encoding="utf-8")

    eng = ContextEngine(td)
    eng.init()

    cf = (td.memory_dir() / "core_facts.md").read_text(encoding="utf-8")
    assert legacy in cf
    # Backup file is created so users can verify migration worked.
    bak = td.path / "memory.txt.v0_1.bak"
    assert bak.exists() and bak.read_text(encoding="utf-8") == legacy
    # The legacy memory.txt is left empty so v0.1 readers don't see duplicates.
    assert td.memory_md_path().read_text(encoding="utf-8") == ""


def test_migration_does_not_overwrite_existing_core_facts(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-no-overwrite")
    td.init()
    td.memory_md_path().write_text("legacy", encoding="utf-8")
    # Pre-seed core_facts.md with already-curated content.
    td.memory_dir().mkdir(parents=True, exist_ok=True)
    (td.memory_dir() / "core_facts.md").write_text("hand-curated\n", encoding="utf-8")

    ContextEngine(td).init()

    cf = (td.memory_dir() / "core_facts.md").read_text(encoding="utf-8")
    assert "hand-curated" in cf
    assert "legacy" not in cf


# ---------------------------------------------------------------------------
# append + snapshot roundtrip
# ---------------------------------------------------------------------------

def test_append_history_and_snapshot_roundtrip(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-rt")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    eng.append_history({"cycle": 1, "phase": "research", "summary": "first"})
    eng.append_history({"cycle": 1, "phase": "verify", "summary": "ok", "score": 0.7})
    eng.append_history({"cycle": 2, "phase": "verify", "summary": "better", "score": 0.85})

    snap = eng.snapshot()
    assert isinstance(snap, MemorySnapshot)
    assert snap.history_count == 3
    # Episodic / core_facts may still be empty before compact() — that's fine.
    rendered = snap.render()
    assert "Episodic" in rendered and "Core Facts" in rendered


# ---------------------------------------------------------------------------
# compactor
# ---------------------------------------------------------------------------

def test_compact_emits_episodic_lines(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-compact")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    for cycle, phase, summary, score in [
        (1, "research", "look up binary search", None),
        (1, "verify", "passes 4 of 5 tests", 0.6),
        (2, "verify", "passes all tests", 0.95),
    ]:
        rec = {"cycle": cycle, "phase": phase, "summary": summary}
        if score is not None:
            rec["score"] = score
        eng.append_history(rec)

    info = eng.compact()
    assert info["lines_kept"] >= 3
    body = (td.memory_dir() / "episodic.md").read_text(encoding="utf-8")
    assert "research" in body and "verify" in body
    # ★best marker on the highest-score line.
    assert "★best" in body


def test_compact_extracts_core_facts_from_hint(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-core")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    eng.append_history({
        "cycle": 1,
        "phase": "judge",
        "summary": "passed",
        "hint": "CORE: always include empty-list edge case\nplain hint that should not migrate",
    })
    eng.append_history({
        "cycle": 2,
        "phase": "judge",
        "summary": "again",
        "hint": "CORE: always include empty-list edge case",  # duplicate
    })

    info = eng.compact()
    assert info["core_extracted"] == 1  # duplicate filtered

    cf = (td.memory_dir() / "core_facts.md").read_text(encoding="utf-8")
    assert "CORE: always include empty-list edge case" in cf
    assert "plain hint that should not migrate" not in cf


# ---------------------------------------------------------------------------
# sensors
# ---------------------------------------------------------------------------

def test_sensors_duplicate_ratio(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-sens-dup")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    # Manually craft an episodic.md with 4 lines, 2 duplicates.
    (td.memory_dir() / "episodic.md").write_text(
        "- alpha\n- beta\n- alpha\n- beta\n", encoding="utf-8"
    )
    s = eng.sensors()
    # 2 duplicates out of 4 lines.
    assert s["duplicate_ratio"] == pytest.approx(0.5)
    assert s["contradiction_count"] == 0


def test_sensors_relevance_decreases_with_size(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-sens-rel")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    # Empty -> relevance 1.0
    assert eng.sensors()["relevance_score"] == pytest.approx(1.0)

    # Half-full bytes (~4KB out of 8KB budget) -> ~0.5
    (td.memory_dir() / "episodic.md").write_text("x" * 4096, encoding="utf-8")
    assert 0.4 <= eng.sensors()["relevance_score"] <= 0.6

    # Over budget -> 0.0
    (td.memory_dir() / "episodic.md").write_text("x" * 16384, encoding="utf-8")
    assert eng.sensors()["relevance_score"] == 0.0


def test_sensors_staleness(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id="ctx-stale")
    td.init()
    eng = ContextEngine(td)
    eng.init()

    eng.append_history({"cycle": 1, "phase": "research", "summary": "old"})
    eng.append_history({"cycle": 5, "phase": "verify", "summary": "newer"})

    assert eng.sensors()["staleness_age_cycles"] == 4
