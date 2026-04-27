# agent-loop-cli — Architecture (v0.2)

This document is a focused extract of `docs/plan-v0.1.md` section 5, plus the v0.2
Context Engine, the v0.2 multi-axis Verify Engine, and the locations where v0.3+
components plug in. See `docs/plan-v0.2.md` for design rationale.

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
                          |  + compact() per cycle  |
                          |  src/.../orchestrator.py|
                          +-----------+-------------+
                                      |
                                      v
              +---------------------------------------------+
              |  Phase Workers (5 stateless functions)      |
              |   run_research / run_plan / run_implement / |
              |   run_verify  / run_judge                   |
              |  src/agent_loop/workers.py                  |
              +-+---------+-----------+-----------+---------+
                |         |           |           |
                v         v           v           v
   +----------------+ +-----------+ +-------+ +------------+
   | Model Router  | | Context   | | State | | Verify     |
   | litellm +     | | Engine    | | Store | | Engine     |
   | 3 CLIs        | | 3-tier    | |       | | (v0.2)     |
   | src/.../      | | memory    | |TaskDir| | rubric +   |
   | models.py     | | src/.../  | |       | | evaluators/|
   |               | | context.py| |       | |            |
   +-------+-------+ +-----+-----+ +---+---+ +-----+------+
           |               |           |           |
           v               v           v           v
   +-----------------+ +-----------+ +--------+ +---------------+
   | litellm + 3 CLI | | memory/   | |.agent_ | | evaluators/   |
   | adapters:       | |  history  | | loop/  | |  pytest_      |
   |  cursor/<m>     | |  .jsonl   | |  <id>/ | |  benchmark    |
   |  claude/<m>     | |  episodic | |        | |  ast_grep     |
   |  gemini/<m>     | |  core_    | |        | |  llm_rubric   |
   | (all subprocess)| |  facts.md | |        | |               |
   +-----------------+ +-----------+ +--------+ +---------------+
```

Eight modules, four real layers. Four of them (Model Router, Context Engine,
State Store, Verify Engine) are leaves the workers share, not separate hops the
orchestrator goes through. Verify Engine in turn dispatches to ``evaluators/*``.

## 2. Module responsibilities

| Module | Responsibility | Key types |
|---|---|---|
| `cli.py` | typer CLI surface: `run / list / resume / bench / config / models`. Owns rich console output. | `app` (Typer) |
| `orchestrator.py` | The R->P->I->V->J loop. Resume from checkpoint, rollback on judge regression, budget guard. | `Orchestrator`, `RunResult` |
| `workers.py` | One pure function per phase. Reads + writes `TaskDir`, calls the model router. **No state across calls.** | `run_research / run_plan / run_implement / run_verify / run_judge` |
| `models.py` | Single `call_model(phase, prompt, system, config)` entry point. Dispatches CLI model ids (`cursor/<m>`, `claude/<m>`, `gemini/<m>`) to local subprocess adapters (`_call_cursor_cli` / `_call_claude_cli` / `_call_gemini_cli` — no API key, uses each CLI's own login) via the `_cli_provider()` helper; everything else goes to litellm (`_call_litellm`). Tracks tokens / cost / latency; one retry on rate-limit. | `ModelResponse`, `call_model`, `_call_cursor_cli`, `_call_claude_cli`, `_call_gemini_cli` |
| `context.py` | v0.2 Context Engine. Owns the 3-tier `memory/` layout (`history.jsonl` + `episodic.md` + `core_facts.md`), rule-based Compactor, and sensor heuristics (`duplicate_ratio`, `contradiction_count`, `staleness_age_cycles`, `relevance_score`). Migrates v0.1 `memory.txt` once. No LLM calls. | `ContextEngine`, `MemorySnapshot` |
| `verify_engine.py` + `evaluators/*` | v0.2 multi-axis Verify Engine. When a task ships a `rubric.json`, drives axes through `pytest_runner` / `benchmark` / `ast_grep` (ground-truth) and `llm_rubric` (soft fallback). Backward compat: rubric absent -> `_run_verify_llm_legacy`. | `VerifyEngine`, `AxisScore`, `VerifyResult`, `yaml_to_rubric` |
| `config.py` | TOML loader (file + env override) + pydantic validation. | `Config`, `Models`, `Budget`, `Runtime` |
| `state.py` | All file IO under `.agent_loop/<task-id>/`. Artifacts, checkpoints, metrics, workspace, memory directory. | `TaskDir`, `list_tasks`, `new_task_id` |
| `prompts/*.md` | Five prompt templates with explicit placeholders rendered by `str.format`. | five files |

## 3. Phase contract (worker -> worker)

Each phase reads named files and writes one named artifact. This is the public contract;
forking a single worker is the recommended extension point.

| Phase | Reads | Writes |
|---|---|---|
| Research | `task.md`, `memory.txt` | `artifacts/findings.md` |
| Plan | `task.md`, `memory.txt`, `findings.md` | `artifacts/plan.md` |
| Implement | `plan.md` (+ `best_solution_summary` if present), `workspace/` | `artifacts/execution_log.md`, `workspace/solution.py` |
| Verify | `execution_log.md`, `workspace/`, import-check sandbox, *optional* `artifacts/rubric.json` | `artifacts/solution.json` (v0.2: `axes` list + `weighted_score` + `summary`; v0.1 schema still accepted) |
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

## 6. Where v0.3+ components plug in

The current module split was deliberately left at the smallest layer count that still
makes sense (KISS / YAGNI; see `progress.txt`). Future work has known extraction points:

```
   Phase Workers
        |
        +-- [DONE v0.2] Context Engine -- 3-tier memory + sensors + rule-based Compactor
        |                                  src/agent_loop/context.py
        |
        +-- [DONE v0.2] Verify Engine  -- multi-axis rubric scoring (pytest /
        |                                  benchmark / ast_grep / llm_rubric)
        |                                  src/agent_loop/verify_engine.py
        |                                  src/agent_loop/evaluators/*
        |
        +-- (v0.3) Judge Strategy     -- multi-judge consensus / cross-vendor voting
        |                                currently single-call: workers.py:run_judge
        |
        +-- (v0.3) Plan Strategy      -- multi-strategy parallel proposals
        |                                currently single-call: workers.py:run_plan
        |
        +-- (v0.3) LLM Compactor      -- swap rule-based body of context.compact()
        |                                with an LLM-backed summarizer behind same iface
        |
        +-- (v0.4) Tool / MCP Bridge  -- workers.py:run_implement currently writes solution.py
                                         only; agent-style tool use needs a layer below
```

Each of these stays a *function call* until a real second implementation exists.
Until then, the prompts + worker functions are the abstraction.

## 7. Non-goals (v0.2)

Explicitly **not** abstracted yet, to avoid premature interfaces:

- Multi-judge / multi-strategy code paths.
- LLM-backed Compactor (v0.2 is rule-based; v0.3 swaps it in).
- LLM-backed sensor metrics (`contradiction_count` / `relevance_score`); v0.2 are
  cheap heuristics with `contradiction_count` returning 0.
- Pluggable retrieval / RAG.
- MCP server mode.
- Cross-task memory store.

These all live in section 4 of `docs/plan-v0.1.md` and section 4 of `docs/plan-v0.2.md`.
