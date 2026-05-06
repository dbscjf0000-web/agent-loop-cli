"""Phase 1 — decision log unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_loop.state import TaskDir, new_task_id


def _td(tmp_path: Path) -> TaskDir:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()
    return td


def test_decision_log_written_with_kv(tmp_path: Path) -> None:
    td = _td(tmp_path)
    td.append_decision("judge", cycle=1, action="stop", score="0.94")
    text = td.decision_log_path().read_text(encoding="utf-8")
    assert "judge" in text
    assert "cycle=1" in text
    assert "action=stop" in text
    assert "score=0.94" in text


def test_decision_log_appends(tmp_path: Path) -> None:
    td = _td(tmp_path)
    td.append_decision("judge", cycle=1, action="continue")
    td.append_decision("judge", cycle=2, action="stop")
    lines = td.decision_log_path().read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert "cycle=1" in lines[0]
    assert "cycle=2" in lines[1]


def test_decision_log_iso_timestamp(tmp_path: Path) -> None:
    td = _td(tmp_path)
    td.append_decision("plan")
    text = td.decision_log_path().read_text(encoding="utf-8")
    # [YYYY-MM-DDTHH:MM:SSZ] phase
    import re
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] plan", text)


def test_decision_log_disabled_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LOOP_DISABLE_DECISION_LOG", "1")
    td = _td(tmp_path)
    td.append_decision("judge", cycle=1)
    assert not td.decision_log_path().exists()
