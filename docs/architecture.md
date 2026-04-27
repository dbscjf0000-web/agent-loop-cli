# agent-loop-cli — Architecture (v0.1.0)

This document is a focused extract of `docs/plan-v0.1.md` section 5, plus the locations where
v0.2+ components plug in.

## 1. Layered view

```
                          +-------------------------+
   user / CI / shell ---->|  CLI (typer)            |
                          |  src/agent_loop/cli.py  |
                          +-----------+-------------+
                                      |
                                      v
                          +-------------------------+
                          |  Orchestrator           |
                          |  R->P->I->V->J loop     |
                          |  rollback + checkpoint  |
                          |  src/.../orchestrator.py|
                          +-----------+-------------+
                                      |
                                      v
              +---------------------------------------------+
              |  Phase Workers (5 stateless functions)      |
              |   run_research / run_plan / run_implement / |
              |   run_verify  / run_judge                   |
              |  src/agent_loop/workers.py                  |
              +-----------+--------------------+------------+
                          |                    |
            (LLM call)    v                    v   (file I/O)
              +-----------------------+   +----------------------+
              |  Model Router         |   |  State Store         |
              |  (litellm wrapper)    |   |  TaskDir             |
              |  src/.../models.py    |   |  src/.../state.py    |
              +-----------+-----------+   +-----------+----------+
                          |                           |
                          v                           v
              +-----------------------+   +----------------------+
              |  Two provider paths:  |   |  .agent_loop/<id>/   |
              |   - litellm.completion|   |  task.md, artifacts/,|
              |     (Anthropic/OpenAI/|   |  workspace/, ...     |
              |      Gemini/Azure/    |   +----------------------+
              |      Ollama)          |
              |   - cursor-agent CLI  |
              |     (subprocess,      |
              |      `cursor/<m>`)    |
              +-----------------------+
```

Six modules, four real layers. Two of them (Model Router, State Store) are leaves the workers
share, not separate hops the orchestrator goes through.

## 2. Module responsibilities

| Module | Responsibility | Key types |
|---|---|---|
| `cli.py` | typer CLI surface: `run / list / resume / bench / config / models`. Owns rich console output. | `app` (Typer) |
| `orchestrator.py` | The R->P->I->V->J loop. Resume from checkpoint, rollback on judge regression, budget guard. | `Orchestrator`, `RunResult` |
| `workers.py` | One pure function per phase. Reads + writes `TaskDir`, calls the model router. **No state across calls.** | `run_research / run_plan / run_implement / run_verify / run_judge` |
| `models.py` | Single `call_model(phase, prompt, system, config)` entry point. Routes `cursor/...` model ids to the local `cursor-agent` CLI (`_call_cursor_cli`, subprocess, no API key) and everything else to litellm (`_call_litellm`); tracks tokens / cost / latency; one retry on rate-limit. | `ModelResponse`, `call_model`, `_call_cursor_cli` |
| `config.py` | TOML loader (file + env override) + pydantic validation. | `Config`, `Models`, `Budget`, `Runtime` |
| `state.py` | All file IO under `.agent_loop/<task-id>/`. Artifacts, checkpoints, metrics, workspace. | `TaskDir`, `list_tasks`, `new_task_id` |
| `prompts/*.md` | Five prompt templates with explicit placeholders rendered by `str.format`. | five files |

## 3. Phase contract (worker -> worker)

Each phase reads named files and writes one named artifact. This is the public contract;
forking a single worker is the recommended extension point.

| Phase | Reads | Writes |
|---|---|---|
| Research | `task.md`, `memory.txt` | `artifacts/findings.md` |
| Plan | `task.md`, `memory.txt`, `findings.md` | `artifacts/plan.md` |
| Implement | `plan.md` (+ `best_solution_summary` if present), `workspace/` | `artifacts/execution_log.md`, `workspace/solution.py` |
| Verify | `execution_log.md`, `workspace/`, import-check sandbox | `artifacts/solution.json` |
| Judge | `solution.json`, `best_solution.json`, `memory.txt`, redo counter | `artifacts/judge_result.json` |

Per-phase `ModelResponse` (tokens, cost, latency, model name) is appended to
`telemetry/metrics.jsonl` and snapshotted into a checkpoint
`checkpoints/cycle_NNN_phase_X.json`.

## 4. Judge decision schema

```json
{
  "better": true,
  "action": "stop",
  "reason": "...",
  "hint": "...",
  "scores": { "this_cycle": 0.97, "best": 0.92 }
}
```

The orchestrator routes on `action` (`stop` / `redo_R` / `redo_P`) and uses `better` to
promote `solution.* -> best_solution.*` or roll back the other way. `redo_count` increments
on every non-improvement; hitting `runtime.max_redo` ends the run.

## 5. Stateless, file-based, resumable

Three properties that drive most of the design:

- **Stateless workers.** Each phase function takes `(TaskDir, Config)` and returns a
  `ModelResponse`. No instance attributes, no closures over previous results — every input is
  read fresh from disk. Restarting from any phase boundary is mechanical.
- **File-based state.** No in-memory orchestration graph. The next phase to run is implied
  by whichever checkpoint is newest. `list_tasks` walks the filesystem and reconstructs a
  task list from artifacts.
- **Resumable.** `Orchestrator._resume_state()` reads the latest checkpoint, derives
  `(start_cycle, start_phase, redo_count, total_cost)`, and re-enters the loop. If the last
  checkpoint was the judge of cycle N, resume at cycle N+1 from research; otherwise
  resume at the next phase of cycle N.

## 6. Where v0.2+ components plug in

The current four-module split was deliberately left at the smallest layer count that still
makes sense (KISS / YAGNI; see `progress.txt`). Future work has known extraction points:

```
   Phase Workers
        |
        +-- (v0.2) Context Engine     -- intent of "what context goes into prompt"
        |                                currently inlined: workers.py read_artifact + memory
        |
        +-- (v0.2) Verify Engine      -- multi-axis rubric scoring
        |                                currently inlined: workers.py:run_verify
        |
        +-- (v0.3) Judge Strategy     -- multi-judge consensus / cross-vendor voting
        |                                currently single-call: workers.py:run_judge
        |
        +-- (v0.3) Plan Strategy      -- multi-strategy parallel proposals
        |                                currently single-call: workers.py:run_plan
        |
        +-- (v0.4) Tool / MCP Bridge  -- workers.py:run_implement currently writes solution.py
                                         only; agent-style tool use needs a layer below
```

Each of these stays a *function call* in v0.1 and only becomes a separate module when a
real second implementation exists. Until then, the prompts + worker functions are the
abstraction.

## 7. Non-goals (v0.1)

Explicitly **not** abstracted yet, to avoid premature interfaces:

- Multi-judge / multi-strategy code paths.
- 3-tier memory (procedural / episodic / semantic). Memory is a single `memory.txt`.
- Pluggable retrieval / RAG.
- MCP server mode.
- Cross-task memory store.

These all live in section 4 of `docs/plan-v0.1.md`.
