"""File-based task state under .agent_loop/<task_id>/.

Files-as-state principle: nothing lives in memory. Every artifact, checkpoint,
and metric is on disk so workers can be killed and resumed at any time.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def new_task_id(nbytes: int = 3) -> str:
    """Short hex id (default 6 chars). Plenty for human-scale task counts."""
    return secrets.token_hex(nbytes)


@dataclass(frozen=True)
class TaskInfo:
    task_id: str
    path: Path
    created_at: float  # mtime of the dir as a cheap "created" approximation


class TaskDir:
    """Filesystem layout owner for a single task.

    Layout under `<root>/<task_id>/`:
      task.md
      memory.txt          (v0.1 legacy; ContextEngine migrates to memory/core_facts.md)
      memory/             (v0.2: 3-tier — history.jsonl + episodic.md + core_facts.md)
      workspace/
      artifacts/
      checkpoints/
      telemetry/metrics.jsonl
    """

    def __init__(self, root: Path, task_id: str) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.path = self.root / task_id

    # --- structure -------------------------------------------------------
    def init(self) -> None:
        for sub in ("artifacts", "workspace", "checkpoints", "telemetry", "memory"):
            (self.path / sub).mkdir(parents=True, exist_ok=True)
        # Touch task.md and memory.txt if missing so callers can append safely.
        for f in (self.task_md_path(), self.memory_md_path()):
            if not f.exists():
                f.touch()
        metrics = self._metrics_path()
        if not metrics.exists():
            metrics.touch()

    # --- paths -----------------------------------------------------------
    def task_md_path(self) -> Path:
        return self.path / "task.md"

    def memory_md_path(self) -> Path:
        """Legacy v0.1 single-file memory. ContextEngine migrates this on init.

        Kept for backward compatibility with existing task directories. New
        code should go through ``ContextEngine.snapshot()`` instead.
        """
        return self.path / "memory.txt"

    def memory_dir(self) -> Path:
        """v0.2 3-tier memory directory (`history.jsonl` + `episodic.md` + `core_facts.md`)."""
        return self.path / "memory"

    def workspace_path(self) -> Path:
        return self.path / "workspace"

    def _artifacts_dir(self) -> Path:
        return self.path / "artifacts"

    def _checkpoints_dir(self) -> Path:
        return self.path / "checkpoints"

    def _metrics_path(self) -> Path:
        return self.path / "telemetry" / "metrics.jsonl"

    # --- artifacts -------------------------------------------------------
    def write_artifact(self, name: str, content: str | dict[str, Any]) -> Path:
        path = self._artifacts_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def read_artifact(self, name: str) -> str | dict[str, Any]:
        """Return parsed JSON for .json files, raw text otherwise."""
        path = self._artifacts_dir() / name
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return json.loads(text)
        return text

    def has_artifact(self, name: str) -> bool:
        return (self._artifacts_dir() / name).exists()

    def artifact_path(self, name: str) -> Path:
        """Absolute path to an artifact by name (does not require existence).

        Used by v0.2+ engines (e.g. VerifyEngine) that want to read or write
        a known artifact without first calling ``has_artifact``.
        """
        return self._artifacts_dir() / name

    # --- metrics ---------------------------------------------------------
    def append_metric(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        path = self._metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --- checkpoints -----------------------------------------------------
    def save_checkpoint(self, cycle: int, phase: str, payload: dict[str, Any]) -> Path:
        name = f"cycle_{cycle:03d}_phase_{phase}.json"
        path = self._checkpoints_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"cycle": cycle, "phase": phase, "ts": time.time(), "payload": payload}
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def load_latest_checkpoint(self) -> dict[str, Any] | None:
        d = self._checkpoints_dir()
        if not d.exists():
            return None
        files = sorted(d.glob("cycle_*_phase_*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))


def list_tasks(root: Path) -> list[TaskInfo]:
    """Discover task directories under `root` (one level deep)."""
    root = Path(root)
    if not root.exists():
        return []
    out: list[TaskInfo] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # Heuristic: a task dir has at least one of these subdirs
        if any((child / sub).exists() for sub in ("artifacts", "checkpoints", "telemetry")):
            out.append(TaskInfo(task_id=child.name, path=child, created_at=child.stat().st_mtime))
    return out
