from __future__ import annotations

import json
from pathlib import Path

from agent_loop.state import TaskDir, list_tasks, new_task_id


def test_taskdir_roundtrip(tmp_path: Path) -> None:
    td = TaskDir(root=tmp_path, task_id=new_task_id())
    td.init()

    # Directory layout
    for sub in ("artifacts", "workspace", "checkpoints", "telemetry"):
        assert (td.path / sub).is_dir()
    assert td.task_md_path().exists()
    assert td.memory_md_path().exists()

    # String artifact
    td.write_artifact("findings.md", "hello world")
    assert td.read_artifact("findings.md") == "hello world"

    # JSON artifact
    payload = {"better": True, "scores": {"correctness": 1.0}}
    td.write_artifact("judge_result.json", payload)
    loaded = td.read_artifact("judge_result.json")
    assert loaded == payload

    # Metric append
    td.append_metric({"phase": "research", "tokens": 123, "cost_usd": 0.001})
    td.append_metric({"phase": "plan", "tokens": 456, "cost_usd": 0.002})
    lines = (td.path / "telemetry" / "metrics.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["phase"] == "research"
    assert "ts" in json.loads(lines[1])

    # Checkpoint roundtrip
    td.save_checkpoint(cycle=1, phase="research", payload={"step": "done"})
    td.save_checkpoint(cycle=2, phase="plan", payload={"step": "drafted"})
    latest = td.load_latest_checkpoint()
    assert latest is not None
    assert latest["cycle"] == 2
    assert latest["phase"] == "plan"
    assert latest["payload"] == {"step": "drafted"}


def test_list_tasks(tmp_path: Path) -> None:
    a = TaskDir(root=tmp_path, task_id="aaaaaa")
    a.init()
    b = TaskDir(root=tmp_path, task_id="bbbbbb")
    b.init()
    # Non-task sibling dir should be ignored
    (tmp_path / "not_a_task").mkdir()

    found = {info.task_id for info in list_tasks(tmp_path)}
    assert found == {"aaaaaa", "bbbbbb"}


def test_new_task_id_distinct() -> None:
    ids = {new_task_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 6 for i in ids)
