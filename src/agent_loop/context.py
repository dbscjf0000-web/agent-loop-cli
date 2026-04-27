"""ContextEngine — 3-tier memory + sensors + rule-based compactor (v0.2).

Layout under ``<task_dir>/memory/``::

    history.jsonl     append-only audit trail (one JSON per phase)
    episodic.md       Compactor output: per-cycle one-liners + score deltas
    core_facts.md     persistent patterns (CORE: lines or migrated v0.1 memory)

Design notes
------------
- **Stateless leaf, like Model Router.** Workers and the orchestrator
  instantiate a ``ContextEngine`` per call; nothing is cached in memory.
  Every method reads / writes the disk afresh.
- **No LLM cost in v0.2.** The Compactor is rule-based; sensors are simple
  heuristics. v0.3 may swap in an LLM-backed compactor / contradiction
  detector behind the same interface.
- **Backward compatible.** ``init()`` migrates a v0.1 ``memory.txt`` into
  ``core_facts.md`` exactly once and leaves a ``memory.txt.v0_1.bak`` behind
  for forensics. Re-running ``init()`` is idempotent.

The orchestrator calls ``compact()`` after every cycle and records
``sensors()`` into ``telemetry/metrics.jsonl`` under the ``quality`` key.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class MemorySnapshot:
    """Phase-prompt input. ``ContextEngine.snapshot()`` returns this.

    v0.4 adds ``global_patterns`` — a slice of the user-global ``patterns.md``
    that is appended (when non-empty) so workers see prior cross-task learning.
    """

    episodic: str
    core_facts: str
    history_count: int
    global_patterns: str = ""  # v0.4: cross-task patterns from ~/.agent-loop/global/

    def render(self) -> str:
        """Default rendering used by phase workers as the ``{memory}`` slot.

        Conditionally appends a ``# Global Patterns (cross-task)`` section when
        ``global_patterns`` is non-empty, so v0.3 prompts that never saw the
        section continue to render byte-identically when the feature is off.
        """
        ep = self.episodic.strip() or "(none)"
        cf = self.core_facts.strip() or "(none)"
        gp = self.global_patterns.strip()
        out = f"# Episodic\n{ep}\n\n# Core Facts\n{cf}"
        if gp:
            out += f"\n\n# Global Patterns (cross-task)\n{gp}"
        return out


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

# Compactor knobs. Conservative defaults — better to keep one extra line than
# to lose a hint. v0.3 may tune these based on sensor feedback.
_COMPACT_TRIGGER_BYTES = 6 * 1024
_EPISODIC_KEEP_LINES = 200  # hard cap regardless of trigger
_RELEVANCE_BUDGET_BYTES = 8 * 1024  # used by relevance heuristic only

_CORE_PREFIX = "CORE:"

# Match `cycle N` at end-of-line for compactor grouping.
_CYCLE_RE = re.compile(r"cycle\s+(\d+)", re.IGNORECASE)

# v0.4 cross-task memory defaults. The orchestrator passes runtime config
# overrides through ``ContextEngine.__init__`` so these are only the ultimate
# fallback (e.g. when a worker constructs an engine without a Config in scope).
_DEFAULT_GLOBAL_ROOT = "~/.agent-loop/global"
_DEFAULT_GLOBAL_MAX_CHARS = 4000


class ContextEngine:
    """3-tier memory + rule-based compactor + sensor heuristics.

    All methods are safe to call on a partially-initialised task directory:
    ``init()`` is idempotent and the readers tolerate missing files.

    v0.4 adds optional cross-task memory. When ``cross_task=True`` (default),
    ``snapshot()`` includes a slice of ``<global_root>/patterns.md`` and
    ``commit_to_global()`` (typically called by the orchestrator at run end)
    appends new ``CORE:`` lines + a one-line task summary to that directory.
    Disabling ``cross_task`` reverts to v0.3 behaviour exactly.
    """

    def __init__(
        self,
        task_dir: TaskDir,
        *,
        global_root: Path | str | None = None,
        cross_task: bool = True,
        global_max_chars: int = _DEFAULT_GLOBAL_MAX_CHARS,
    ) -> None:
        self.task_dir = task_dir
        # ``global_root`` is stored unresolved; ``_global_dir()`` expands ~ at
        # access time so a config change between construction and call is
        # honoured (rare, but matches the rest of the engine's lazy-IO style).
        self._global_root = global_root if global_root is not None else _DEFAULT_GLOBAL_ROOT
        self._cross_task = bool(cross_task)
        self._global_max_chars = int(global_max_chars)

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------
    @property
    def _dir(self) -> Path:
        return self.task_dir.memory_dir()

    @property
    def _history_path(self) -> Path:
        return self._dir / "history.jsonl"

    @property
    def _episodic_path(self) -> Path:
        return self._dir / "episodic.md"

    @property
    def _core_facts_path(self) -> Path:
        return self._dir / "core_facts.md"

    @property
    def _legacy_memory_path(self) -> Path:
        return self.task_dir.memory_md_path()

    @property
    def _legacy_backup_path(self) -> Path:
        return self.task_dir.path / "memory.txt.v0_1.bak"

    # ------------------------------------------------------------------
    # v0.4: cross-task / global memory paths
    # ------------------------------------------------------------------
    def _global_dir(self) -> Path:
        """Expanded global directory (e.g. ``~/.agent-loop/global``).

        Always returns a Path; never creates the directory (callers do that
        explicitly when committing — readers tolerate absence).
        """
        return Path(str(self._global_root)).expanduser()

    def _global_patterns_path(self) -> Path:
        return self._global_dir() / "patterns.md"

    def _global_index_path(self) -> Path:
        return self._global_dir() / "task_index.jsonl"

    # ------------------------------------------------------------------
    # init / migration
    # ------------------------------------------------------------------
    def init(self) -> None:
        """Create ``memory/`` + the three files; migrate v0.1 ``memory.txt`` once.

        Idempotent. Safe on resume.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        for p in (self._history_path, self._episodic_path, self._core_facts_path):
            if not p.exists():
                p.touch()

        # v0.1 -> v0.2 migration. Only runs when:
        #   - legacy memory.txt is non-empty
        #   - core_facts.md is empty (we don't overwrite the new layout)
        #   - the backup file does not yet exist (don't migrate twice)
        legacy = self._legacy_memory_path
        backup = self._legacy_backup_path
        if (
            legacy.exists()
            and legacy.read_text(encoding="utf-8").strip()
            and self._core_facts_path.read_text(encoding="utf-8").strip() == ""
            and not backup.exists()
        ):
            content = legacy.read_text(encoding="utf-8")
            header = (
                "# Core Facts (migrated from v0.1 memory.txt)\n"
                f"# migrated_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n"
            )
            self._core_facts_path.write_text(header + content, encoding="utf-8")
            backup.write_text(content, encoding="utf-8")
            # leave legacy memory.txt empty so v0.1 readers see "no memory" rather
            # than the duplicated migrated content
            legacy.write_text("", encoding="utf-8")

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def snapshot(self) -> MemorySnapshot:
        """Return current episodic + core_facts (+ optional global) for prompt rendering.

        Tolerates missing files (returns empty strings). ``init()`` is the
        canonical creator but callers should not assume it ran.

        v0.4: when ``cross_task=True`` is set on the engine, also reads a
        bounded slice of ``<global_root>/patterns.md`` (most recent
        ``global_max_chars`` bytes). The slice is empty if the global file
        does not exist, so cross-task can be safely enabled before any task
        has committed.
        """
        ep = self._read_or_empty(self._episodic_path)
        cf = self._read_or_empty(self._core_facts_path)
        # Migrated content might not have happened yet; surface the legacy
        # memory.txt as a fallback so the very first run still sees prior hints.
        if not cf.strip():
            legacy = self._read_or_empty(self._legacy_memory_path)
            if legacy.strip():
                cf = legacy
        count = self._count_lines(self._history_path)
        gp = self._load_global_patterns(self._global_max_chars) if self._cross_task else ""
        return MemorySnapshot(
            episodic=ep, core_facts=cf, history_count=count, global_patterns=gp
        )

    # ------------------------------------------------------------------
    # v0.4: cross-task / global memory I/O
    # ------------------------------------------------------------------
    def _load_global_patterns(self, max_chars: int | None = None) -> str:
        """Return the trailing ``max_chars`` of ``patterns.md`` (or empty).

        Trailing slice (newest first by append order) keeps the most recently
        committed patterns when the file outgrows the budget. ``max_chars=None``
        means "use the engine default". Honors the ``cross_task`` flag — if
        cross-task is disabled, returns ``""`` without touching disk.
        """
        if not self._cross_task:
            return ""
        budget = self._global_max_chars if max_chars is None else int(max_chars)
        text = self._read_or_empty(self._global_patterns_path())
        if not text:
            return ""
        if budget <= 0 or len(text) <= budget:
            return text
        # Slice from the end and snap to the next newline so we don't break a
        # CORE: line in half. If no newline survives, fall back to raw slice.
        tail = text[-budget:]
        nl = tail.find("\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1 :]
        return tail

    def commit_to_global(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Persist this task's CORE: lines + a one-line summary into the global dir.

        Called by the orchestrator at run end (any final_status). Idempotent:
        running it twice for the same ``task_id`` will not duplicate lines in
        ``patterns.md`` (exact-match dedup) nor in ``task_index.jsonl`` (set
        check on prior task_ids).

        Returns a stat dict ``{committed, patterns_added, index_added,
        reason}`` so callers can log or test the outcome. When cross-task is
        disabled, returns ``{"committed": False, "reason": "disabled"}``
        without touching disk.
        """
        if not self._cross_task:
            return {"committed": False, "patterns_added": 0, "index_added": 0, "reason": "disabled"}

        gdir = self._global_dir()
        try:
            gdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover - defensive
            return {"committed": False, "patterns_added": 0, "index_added": 0, "reason": f"mkdir failed: {e}"}

        # --- patterns.md: dedup-append CORE: lines from this task's core_facts.md ---
        local_core = self._read_or_empty(self._core_facts_path)
        local_lines = [
            ln.strip()
            for ln in local_core.splitlines()
            if ln.strip().startswith(_CORE_PREFIX)
        ]
        existing = {
            ln.strip()
            for ln in self._read_or_empty(self._global_patterns_path()).splitlines()
            if ln.strip().startswith(_CORE_PREFIX)
        }
        new_lines = []
        for ln in local_lines:
            if ln not in existing:
                new_lines.append(ln)
                existing.add(ln)  # in-batch dedup too
        if new_lines:
            ppath = self._global_patterns_path()
            tail = ("\n" if (ppath.exists() and ppath.read_text(encoding="utf-8").strip() and not ppath.read_text(encoding="utf-8").endswith("\n")) else "")
            with ppath.open("a", encoding="utf-8") as f:
                f.write(tail + "\n".join(new_lines) + "\n")

        # --- task_index.jsonl: idempotent append (skip if task_id already present) ---
        idx_path = self._global_index_path()
        task_id = str(summary.get("task_id", ""))
        already_indexed = False
        if task_id and idx_path.exists():
            for line in idx_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("task_id", "")) == task_id:
                    already_indexed = True
                    break

        index_added = 0
        if task_id and not already_indexed:
            row = {"timestamp": time.time(), **summary}
            with idx_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            index_added = 1

        return {
            "committed": True,
            "patterns_added": len(new_lines),
            "index_added": index_added,
            "reason": "ok",
        }

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def append_history(self, record: dict[str, Any]) -> None:
        """Append one JSON record to ``history.jsonl`` (audit trail)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), **record}
        with self._history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # compactor
    # ------------------------------------------------------------------
    def compact(self, *, force: bool = False) -> dict[str, Any]:
        """Rebuild ``episodic.md`` + harvest CORE lines into ``core_facts.md``.

        Trigger: ``force=True``, or ``episodic.md`` size > ``_COMPACT_TRIGGER_BYTES``,
        or there is at least one new history record (i.e. always after a cycle).

        Strategy (rule-based, no LLM):
          - Group history records by ``cycle``.
          - Emit one summary line per (cycle, phase) preserving order.
          - Append best-score evolution markers when ``score`` is monotonic.
          - Cap at ``_EPISODIC_KEEP_LINES``; older lines are dropped silently.
          - Any history line carrying ``hint`` text starting with ``CORE:`` is
            appended (deduplicated) to ``core_facts.md``.
        """
        before = self._size_or_zero(self._episodic_path)
        records = self._read_history()
        if not records and not force:
            # Nothing to compact yet (e.g., orchestrator called pre-cycle).
            return {
                "size_before": before,
                "size_after": before,
                "lines_kept": 0,
                "core_extracted": 0,
                "triggered": False,
            }

        # --- episodic rebuild ---
        lines: list[str] = []
        best_so_far: float | None = None
        for rec in records:
            cycle = rec.get("cycle")
            phase = rec.get("phase", "?")
            summary = (rec.get("summary") or "").strip().replace("\n", " ")
            score = rec.get("score")
            if isinstance(summary, str) and len(summary) > 200:
                summary = summary[:200].rstrip() + "..."
            line = f"- [c{cycle:>3}|{phase:<9}] {summary}" if isinstance(cycle, int) else f"- [{phase}] {summary}"
            if isinstance(score, (int, float)):
                line += f" (score={float(score):.3f})"
                if best_so_far is None or float(score) > best_so_far:
                    best_so_far = float(score)
                    line += " ★best"
            lines.append(line)

        # Trim from the front (older) if we exceed the cap.
        if len(lines) > _EPISODIC_KEEP_LINES:
            dropped = len(lines) - _EPISODIC_KEEP_LINES
            lines = [f"- [...] {dropped} older lines dropped by compactor"] + lines[-_EPISODIC_KEEP_LINES:]

        episodic_text = "# Episodic Summary\n\n" + "\n".join(lines) + "\n"
        # Bytes-trigger guard: if we somehow are still over the bytes cap, the
        # rebuild already replaces the file content so it cannot grow.
        self._episodic_path.write_text(episodic_text, encoding="utf-8")

        # --- core facts harvest ---
        core_existing = set(
            ln.strip()
            for ln in self._read_or_empty(self._core_facts_path).splitlines()
            if ln.strip().startswith(_CORE_PREFIX)
        )
        new_core: list[str] = []
        for rec in records:
            hint = (rec.get("hint") or "").strip()
            if not hint:
                continue
            for chunk in hint.splitlines():
                cs = chunk.strip()
                if cs.startswith(_CORE_PREFIX) and cs not in core_existing:
                    new_core.append(cs)
                    core_existing.add(cs)
        if new_core:
            tail = ("\n" if self._core_facts_path.read_text(encoding="utf-8").strip() else "")
            with self._core_facts_path.open("a", encoding="utf-8") as f:
                f.write(tail + "\n".join(new_core) + "\n")

        after = self._size_or_zero(self._episodic_path)
        return {
            "size_before": before,
            "size_after": after,
            "lines_kept": len(lines),
            "core_extracted": len(new_core),
            "triggered": True or force or before > _COMPACT_TRIGGER_BYTES,
        }

    # ------------------------------------------------------------------
    # sensors
    # ------------------------------------------------------------------
    def sensors(self) -> dict[str, Any]:
        """Return cheap context-quality heuristics. v0.2 is LLM-free.

        Keys
        ----
        ``duplicate_ratio`` : float in [0, 1]
            Fraction of episodic lines that already appeared earlier (case-insensitive).
        ``contradiction_count`` : int
            Placeholder, always 0 in v0.2 (LLM-backed in v0.3).
        ``staleness_age_cycles`` : int
            Distance between the oldest and newest cycle in ``history.jsonl``.
        ``relevance_score`` : float in [0, 1]
            Length-bounded heuristic — shorter episodic ⇒ higher score.
            ``1.0 - clamp(len(episodic) / _RELEVANCE_BUDGET_BYTES, 0, 1)``.
        """
        episodic = self._read_or_empty(self._episodic_path)
        records = self._read_history()

        duplicate_ratio = self._duplicate_ratio(episodic)
        contradiction_count = 0  # v0.3 hook
        staleness = self._staleness(records)
        relevance = self._relevance(episodic)

        return {
            "duplicate_ratio": round(duplicate_ratio, 4),
            "contradiction_count": contradiction_count,
            "staleness_age_cycles": staleness,
            "relevance_score": round(relevance, 4),
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _read_or_empty(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

    @staticmethod
    def _size_or_zero(path: Path) -> int:
        try:
            return path.stat().st_size
        except (FileNotFoundError, OSError):
            return 0

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
        except (FileNotFoundError, OSError):
            return 0

    def _read_history(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        text = self._read_or_empty(self._history_path)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # ignore malformed line — audit trail is best-effort
                continue
        return out

    @staticmethod
    def _duplicate_ratio(text: str) -> float:
        lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return 0.0
        seen: set[str] = set()
        dup = 0
        for ln in lines:
            if ln in seen:
                dup += 1
            else:
                seen.add(ln)
        return dup / len(lines)

    @staticmethod
    def _staleness(records: list[dict[str, Any]]) -> int:
        cycles = [r.get("cycle") for r in records if isinstance(r.get("cycle"), int)]
        if not cycles:
            return 0
        return max(cycles) - min(cycles)

    @staticmethod
    def _relevance(text: str) -> float:
        n = len(text.encode("utf-8", errors="ignore"))
        if n <= 0:
            return 1.0
        ratio = n / _RELEVANCE_BUDGET_BYTES
        if ratio >= 1.0:
            return 0.0
        return 1.0 - ratio


__all__ = ["ContextEngine", "MemorySnapshot"]
