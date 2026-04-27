"""CLI tests for v0.4 `agent-loop memory` subcommands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_loop.cli import app

runner = CliRunner()


@pytest.fixture
def tmp_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Override the cross-task memory directory via env var to a tmp location."""
    d = tmp_path / "global"
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(d))
    return d


def test_memory_path_prints_dir(tmp_global: Path) -> None:
    result = runner.invoke(app, ["memory", "path"])
    assert result.exit_code == 0
    assert str(tmp_global) in result.stdout.strip()


def test_memory_show_no_file(tmp_global: Path) -> None:
    """show on an empty / missing dir prints a friendly message, not an error."""
    result = runner.invoke(app, ["memory", "show"])
    assert result.exit_code == 0
    assert "no patterns.md" in result.stdout


def test_memory_show_prints_lines(tmp_global: Path) -> None:
    tmp_global.mkdir(parents=True, exist_ok=True)
    (tmp_global / "patterns.md").write_text(
        "CORE: alpha\nCORE: beta\nCORE: gamma\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["memory", "show", "--limit", "2"])
    assert result.exit_code == 0
    # tail-2: beta + gamma
    assert "CORE: beta" in result.stdout
    assert "CORE: gamma" in result.stdout
    assert "CORE: alpha" not in result.stdout


def test_memory_list_table(tmp_global: Path) -> None:
    tmp_global.mkdir(parents=True, exist_ok=True)
    rows = [
        {"task_id": "abc123", "weighted_score": 0.97, "cycles": 1, "final_status": "stop", "task_md_first_line": "Implement add().", "timestamp": 1.0},
        {"task_id": "def456", "weighted_score": 0.55, "cycles": 3, "final_status": "max_redo", "task_md_first_line": "Solve n_queens.", "timestamp": 2.0},
    ]
    (tmp_global / "task_index.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["memory", "list"])
    assert result.exit_code == 0
    out = result.stdout
    assert "abc123" in out and "def456" in out
    assert "0.970" in out and "0.550" in out
    assert "stop" in out and "max_redo" in out


def test_memory_wipe_with_yes(tmp_global: Path) -> None:
    """--yes skips the confirmation prompt and removes the dir."""
    tmp_global.mkdir(parents=True, exist_ok=True)
    (tmp_global / "patterns.md").write_text("CORE: x\n", encoding="utf-8")
    assert tmp_global.exists()
    result = runner.invoke(app, ["memory", "wipe", "--yes"])
    assert result.exit_code == 0
    assert not tmp_global.exists()


def test_memory_wipe_aborts_without_confirm(tmp_global: Path) -> None:
    """Without --yes and answering 'n' to confirm, dir is preserved."""
    tmp_global.mkdir(parents=True, exist_ok=True)
    (tmp_global / "patterns.md").write_text("CORE: y\n", encoding="utf-8")
    result = runner.invoke(app, ["memory", "wipe"], input="n\n")
    # typer.confirm returning False prints "aborted" and exit 0
    assert result.exit_code == 0
    assert tmp_global.exists()
    assert (tmp_global / "patterns.md").exists()
