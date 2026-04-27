"""R→P→I→V→J orchestration loop.

Worker functions live in workers.py. The orchestrator's job is to:
  - Drive the cycle order, including resume-from-checkpoint.
  - Record metrics + checkpoints.
  - React to the judge's `action` (stop / redo_R / redo_P).
  - Roll back to best_solution.json when a cycle regresses.
  - Bail out on max_cycles, max_redo, or budget.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Literal

from rich.console import Console

from agent_loop.config import Config
from agent_loop.context import ContextEngine
from agent_loop.models import ModelResponse
from agent_loop.state import TaskDir
from agent_loop.workers import (
    run_implement,
    run_judge,
    run_plan,
    run_research,
    run_verify,
)

Mode = Literal["auto", "supervised"]
Phase = Literal["research", "plan", "implement", "verify", "judge"]

_PHASE_ORDER: tuple[Phase, ...] = ("research", "plan", "implement", "verify", "judge")
_PHASE_FUNCS: dict[Phase, Callable[[TaskDir, Config], ModelResponse]] = {
    "research": run_research,
    "plan": run_plan,
    "implement": run_implement,
    "verify": run_verify,
    "judge": run_judge,
}


@dataclass
class RunResult:
    task_id: str
    cycles_run: int
    final_status: str
    best_solution_path: str | None
    total_cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cycles_run": self.cycles_run,
            "final_status": self.final_status,
            "best_solution_path": self.best_solution_path,
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


class Orchestrator:
    def __init__(
        self,
        task_dir: TaskDir,
        config: Config,
        *,
        console: Console | None = None,
        confirm_plan: Callable[[], bool] | None = None,
    ) -> None:
        self.task_dir = task_dir
        self.config = config
        self.console = console or Console()
        self._confirm_plan = confirm_plan
        # v0.2: Context Engine. init() is idempotent (safe on resume + creates
        # the memory/ layout, migrating any legacy memory.txt once).
        # v0.4: pass through cross-task memory config so snapshot() can include
        # a slice of ~/.agent-loop/global/patterns.md and run() can commit at end.
        self.context = ContextEngine(
            task_dir,
            global_root=config.runtime.cross_task_memory_dir,
            cross_task=config.runtime.cross_task_memory,
            global_max_chars=config.runtime.cross_task_memory_max_chars,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def run(
        self,
        task: str,
        *,
        max_cycles: int,
        mode: Mode = "auto",
        max_redo: int = 3,
    ) -> dict[str, Any]:
        self.task_dir.init()
        # ContextEngine layout (3-tier memory) — runs after TaskDir.init() so
        # memory/ is guaranteed to exist before any phase reads from it.
        self.context.init()

        # Persist task.md if not already there (resume case keeps the original).
        if not self.task_dir.task_md_path().read_text(encoding="utf-8").strip():
            self.task_dir.task_md_path().write_text(task, encoding="utf-8")

        start_cycle, start_phase, redo_count, total_cost = self._resume_state()

        cycles_run = 0
        final_status = "unknown"

        for cycle in range(start_cycle, max_cycles + 1):
            cycles_run = cycle
            self.console.print(
                f"[bold cyan]>>> Cycle {cycle}/{max_cycles}[/bold cyan]"
                f" (redo={redo_count}/{max_redo}, cost=${total_cost:.4f})"
            )

            phases_to_run = self._phases_for_cycle(start_phase if cycle == start_cycle else None)

            for phase in phases_to_run:
                resp = self._run_phase(phase, cycle)
                total_cost += resp.cost_usd
                metric = {
                    "cycle": cycle,
                    "phase": phase,
                    "tokens_in": resp.prompt_tokens,
                    "tokens_out": resp.completion_tokens,
                    "cost_usd": resp.cost_usd,
                    "latency_s": resp.latency_s,
                    "model": resp.model,
                }
                # v0.3: surface multi-judge consensus stats on the judge metric row.
                if phase == "judge" and self.task_dir.has_artifact("judge_result.json"):
                    jr = self.task_dir.read_artifact("judge_result.json")
                    if isinstance(jr, dict) and isinstance(jr.get("consensus"), dict):
                        cs = jr["consensus"]
                        metric["n_judges"] = cs.get("n_judges")
                        metric["votes_action"] = cs.get("votes_action")
                        metric["votes_better"] = cs.get("votes_better")
                        metric["consensus_fallback"] = cs.get("fallback", False)
                # v0.3: surface multi-strategy selector stats on the plan metric row.
                if phase == "plan" and self.task_dir.has_artifact("plan_selector.json"):
                    ps = self.task_dir.read_artifact("plan_selector.json")
                    if isinstance(ps, dict):
                        scores = ps.get("scores")
                        metric["n_strategies"] = (
                            len(scores) if isinstance(scores, list) else None
                        )
                        metric["selector_method"] = ps.get("selector_method")
                        metric["winner_index"] = ps.get("winner_index")
                        metric["winner_provider"] = ps.get("winner_provider")
                        if ps.get("selector_method") == "fallback":
                            metric["selector_fallback"] = True
                self.task_dir.append_metric(metric)
                self.task_dir.save_checkpoint(
                    cycle,
                    phase,
                    {
                        "redo_count": redo_count,
                        "total_cost": total_cost,
                        "next_phase": _next_phase(phase),
                    },
                )

                if phase == "plan" and mode == "supervised":
                    if not self._ask_confirm("Plan written. Continue to implement?"):
                        final_status = "user_aborted"
                        return self._finalize(cycles_run, final_status, total_cost, task)

                if total_cost > self.config.budget.per_run_usd:
                    self.console.print(
                        f"[bold red]Budget exceeded[/bold red] (${total_cost:.4f} > "
                        f"${self.config.budget.per_run_usd:.4f})"
                    )
                    final_status = "budget_exceeded"
                    return self._finalize(cycles_run, final_status, total_cost, task)

            # ----- post-cycle judge handling -----
            j = self._read_judge_result()
            this_score = float(((j.get("scores") or {}).get("this_cycle") or 0.0))
            best_score = ((j.get("scores") or {}).get("best"))
            self.console.print(
                f"  judge: better={j.get('better')} action={j.get('action')!r} "
                f"score={this_score:.3f} best={best_score}"
            )

            if j.get("better"):
                self._promote_to_best()
                redo_count = 0
            else:
                self._rollback_to_best()
                redo_count += 1

            # ----- v0.2 Context Engine: compact + sensors -----
            # Run after promote/rollback so the history reflects the final
            # bookkeeping for this cycle, but before the loop exits so the
            # quality metric is recorded for every cycle (including stop).
            try:
                compact_info = self.context.compact()
                quality = self.context.sensors()
                self.task_dir.append_metric(
                    {
                        "cycle": cycle,
                        "phase": "_cycle_quality",
                        "quality": quality,
                        "compact": compact_info,
                    }
                )
            except Exception as e:  # never let context bookkeeping break a run
                self.console.print(f"[yellow]context engine warning: {e}[/yellow]")

            action = j.get("action", "stop")
            if action == "stop":
                final_status = "stop"
                break
            if redo_count >= max_redo:
                final_status = "max_redo"
                break

            # set up next cycle's start phase
            start_phase = "research" if action == "redo_R" else "plan"
        else:
            final_status = "max_cycles"

        return self._finalize(cycles_run, final_status, total_cost, task)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _finalize(
        self, cycles_run: int, final_status: str, total_cost: float, task_text: str
    ) -> dict[str, Any]:
        """Build the RunResult dict and (v0.4) commit cross-task memory.

        ``commit_to_global`` is best-effort — exceptions are caught and logged
        as a console warning so a global-IO hiccup never breaks the run
        contract. Called from every return path of ``run()``.
        """
        result = RunResult(
            self.task_dir.task_id,
            cycles_run,
            final_status,
            self._best_solution_path(),
            total_cost,
        ).as_dict()
        try:
            summary = self._build_global_summary(result, task_text)
            stat = self.context.commit_to_global(summary)
            if stat.get("committed"):
                self.console.print(
                    f"  [dim]global memory: +{stat.get('patterns_added', 0)} patterns, "
                    f"+{stat.get('index_added', 0)} index row[/dim]"
                )
        except Exception as e:  # never break run() over global-IO
            self.console.print(f"[yellow]global memory commit warning: {e}[/yellow]")
        return result

    def _build_global_summary(self, result: dict[str, Any], task_text: str) -> dict[str, Any]:
        """Compose a privacy-conscious one-line summary for task_index.jsonl."""
        first_line = ""
        for line in (task_text or "").splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped[:200]
                break
        # Pull the latest weighted_score from solution.json (best available).
        weighted_score: float | None = None
        if self.task_dir.has_artifact("best_solution.json"):
            best = self.task_dir.read_artifact("best_solution.json")
            if isinstance(best, dict):
                ws = best.get("weighted_score", best.get("score"))
                if isinstance(ws, (int, float)):
                    weighted_score = float(ws)
        elif self.task_dir.has_artifact("solution.json"):
            sol = self.task_dir.read_artifact("solution.json")
            if isinstance(sol, dict):
                ws = sol.get("weighted_score", sol.get("score"))
                if isinstance(ws, (int, float)):
                    weighted_score = float(ws)
        return {
            "task_id": self.task_dir.task_id,
            "weighted_score": weighted_score,
            "cycles": int(result.get("cycles_run", 0)),
            "task_md_first_line": first_line,
            "final_status": result.get("final_status", "unknown"),
        }

    def _run_phase(self, phase: Phase, cycle: int) -> ModelResponse:
        self.console.print(f"  [yellow]>[/yellow] {phase} (cycle {cycle})")
        return _PHASE_FUNCS[phase](self.task_dir, self.config)

    def _phases_for_cycle(self, start_phase: Phase | None) -> list[Phase]:
        if start_phase is None or start_phase == "research":
            return list(_PHASE_ORDER)
        try:
            idx = _PHASE_ORDER.index(start_phase)
        except ValueError:
            return list(_PHASE_ORDER)
        return list(_PHASE_ORDER[idx:])

    def _resume_state(self) -> tuple[int, Phase | None, int, float]:
        """Inspect last checkpoint to figure out where to resume."""
        cp = self.task_dir.load_latest_checkpoint()
        if not cp:
            return 1, None, 0, 0.0
        cycle = int(cp.get("cycle", 1))
        phase = cp.get("phase", "research")
        payload = cp.get("payload") or {}
        next_phase = payload.get("next_phase") or _next_phase(phase)
        redo_count = int(payload.get("redo_count", 0))
        total_cost = float(payload.get("total_cost", 0.0))
        # If the last checkpoint was the judge of cycle N, resume at cycle N+1 from research.
        if phase == "judge":
            return cycle + 1, "research", redo_count, total_cost
        return cycle, next_phase, redo_count, total_cost

    def _read_judge_result(self) -> dict[str, Any]:
        if not self.task_dir.has_artifact("judge_result.json"):
            return {"better": False, "action": "stop", "scores": {}}
        obj = self.task_dir.read_artifact("judge_result.json")
        return obj if isinstance(obj, dict) else {}

    def _promote_to_best(self) -> None:
        if not self.task_dir.has_artifact("solution.json"):
            return
        sol = self.task_dir.read_artifact("solution.json")
        if isinstance(sol, dict):
            self.task_dir.write_artifact("best_solution.json", sol)
        # snapshot the workspace solution file as well
        sol_py = self.task_dir.workspace_path() / "solution.py"
        if sol_py.exists():
            shutil.copy2(sol_py, self.task_dir.workspace_path() / "best_solution.py")

    def _rollback_to_best(self) -> None:
        if not self.task_dir.has_artifact("best_solution.json"):
            return
        best = self.task_dir.read_artifact("best_solution.json")
        if isinstance(best, dict):
            self.task_dir.write_artifact("solution.json", best)
        best_py = self.task_dir.workspace_path() / "best_solution.py"
        sol_py = self.task_dir.workspace_path() / "solution.py"
        if best_py.exists():
            shutil.copy2(best_py, sol_py)

    def _best_solution_path(self) -> str | None:
        p = self.task_dir.workspace_path() / "best_solution.py"
        if p.exists():
            return str(p)
        sol = self.task_dir.workspace_path() / "solution.py"
        return str(sol) if sol.exists() else None

    def _ask_confirm(self, msg: str) -> bool:
        if self._confirm_plan is not None:
            return bool(self._confirm_plan())
        try:
            import typer

            return bool(typer.confirm(msg, default=True))
        except Exception:
            return True


def _next_phase(phase: Phase) -> Phase | None:
    try:
        idx = _PHASE_ORDER.index(phase)
    except ValueError:
        return None
    if idx + 1 >= len(_PHASE_ORDER):
        return None
    return _PHASE_ORDER[idx + 1]


__all__ = ["Orchestrator", "RunResult"]
