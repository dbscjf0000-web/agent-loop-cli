# agent-loop-cli

> Standalone CLI for the R->P->I->V->J agent loop. Multi-model. File-based state. Resume-friendly.

`agent-loop-cli` runs a 5-stage Research -> Plan -> Implement -> Verify -> Judge cycle against any
LLM (Claude / GPT / Gemini / local), with regression-proof rollback and resumable checkpoints.

## Status

**v0.1.1** — feature-complete MVP, 32 unit tests passing, dry-run end-to-end verified for all
four reference benchmarks, **and one live end-to-end run executed via the `cursor-agent` CLI
provider on KISTI Neuron** (1 cycle, judge auto-stop at score 0.973, real `solution.py`
written). Litellm-backed runs (Anthropic / OpenAI / Gemini / Azure / Ollama) still depend on
provider API keys and are exercised by mocked tests only.

See `docs/plan-v0.1.md` for the full spec, `docs/architecture.md` for the layered design, and
`progress.txt` for build history.

## Why

1. **Opinionated R->P->I->V->J cycle** — not a free-form graph; the 5 phases have stable, file-based
   contracts (one artifact per phase), so prompts and validation can target a fixed schema.
2. **Regression rollback + memory accumulation are first-class** — the judge promotes a winning
   solution to `best_solution.json` and rolls back on regressions. You don't have to re-implement
   "did the last cycle make things worse?" from scratch.
3. **CLI-first, state on disk** — `.agent_loop/<task-id>/` is the source of truth. You can `cat`,
   `grep`, edit, or hand-resume any stage. No in-memory orchestrator graph to debug.
4. **Multi-vendor with one config** — per-phase model assignment via litellm. Use Claude for
   Research / Plan, Sonnet for Implement, Haiku for Verify, GPT for Judge — or any combination —
   without code changes.

## Install

```bash
# KISTI Neuron (or any Python 3.10+ environment)
module load python/3.12.4   # KISTI: ignores deprecation warning, redirects to 3.14.2

# Editable install (recommended for v0.1.0)
pip install -e ".[dev]" --user
```

> **KISTI inode quota note.** If your home inode quota is exhausted (`OSError: [Errno 122]`),
> `pip install` cannot create the entry-point script `agent-loop`, but the editable install of
> the source tree still works. **Call the CLI as `python3 -m agent_loop.cli`** in that case.
> See [Troubleshooting](#troubleshooting).

When PyPI publishes land:

```bash
pip install agent-loop-cli      # not yet on PyPI
uv tool install agent-loop-cli  # alternative
```

## Quickstart

```bash
# 1. Drop a default config to ~/.agent-loop/config.toml
python3 -m agent_loop.cli config init

# 2. Inspect / edit the config
python3 -m agent_loop.cli config show
python3 -m agent_loop.cli config edit

# 3. Run a free-form task
python3 -m agent_loop.cli run "Pure Python N-Queens N=8..13, N=13 in <= 1.5s" \
    --cycles 5 --max-redo 3

# 4. List all task directories under .agent_loop/
python3 -m agent_loop.cli list

# 5. Resume a paused task from its last checkpoint
python3 -m agent_loop.cli resume <task-id>

# 6. Run one of the four reference benchmarks
python3 -m agent_loop.cli bench binary_search --cycles 2
python3 -m agent_loop.cli bench --quick           # binary_search only
python3 -m agent_loop.cli bench --dry-run         # parse + write task.md, no LLM
```

Sample output for `bench --dry-run` (verified in build):

```
[run] benchmark=binary_search task_id=bench-binary_search-1c0f40
   task.md written (1075 chars), cycles=3, max_redo=2
   no LLM calls performed
   task dir: /tmp/al/bench-binary_search-1c0f40
```

A live run ends with a Rich table. The example below was executed against the
`cursor-agent` CLI provider (`cursor/auto` for all 5 phases) on KISTI Neuron:

```
[OK] task_id = 1fc5bb
     root    = /tmp/al_e2e/.agent_loop/1fc5bb
     cycles  = 2, mode = auto, max_redo = 1
>>> Cycle 1/2 (redo=0/1, cost=$0.0000)
  > research (cycle 1)        # 60.2s  cursor/auto
  > plan (cycle 1)             # 30.6s  cursor/auto
  > implement (cycle 1)        # 96.2s  cursor/auto  -> solution.py written
  > verify (cycle 1)           # 41.8s  cursor/auto  -> 0.97 weighted score
  > judge (cycle 1)            # skipped: first cycle
  judge: better=True action='stop' score=0.973 best=None

                                Run summary
+-------------------+--------------------------------------+
| key               | value                                |
+-------------------+--------------------------------------+
| task_id           | 1fc5bb                               |
| cycles_run        | 1                                    |
| final_status      | stop                                 |
| best_solution_path| /tmp/.../workspace/best_solution.py  |
| total_cost_usd    | 0.0  (Pro subscription, no metering) |
+-------------------+--------------------------------------+
```

## Configuration

Default location: `~/.agent-loop/config.toml`. Override with `./agent-loop.toml` (project-local)
or `--config <path>` (explicit). Environment variables override individual fields.

```toml
[models]
research  = "anthropic/claude-opus-4-7"
plan      = "anthropic/claude-opus-4-7"
implement = "anthropic/claude-sonnet-4-6"
verify    = "anthropic/claude-haiku-4-5"
judge     = "openai/gpt-5.2"

[budget]
daily_usd   = 10
per_run_usd = 2

[runtime]
sandbox    = true
max_cycles = 10
max_redo   = 3
```

### Environment variables

| Variable | Maps to | Type |
|---|---|---|
| `AGENT_LOOP_MODEL_RESEARCH` | `[models].research` | string |
| `AGENT_LOOP_MODEL_PLAN` | `[models].plan` | string |
| `AGENT_LOOP_MODEL_IMPLEMENT` | `[models].implement` | string |
| `AGENT_LOOP_MODEL_VERIFY` | `[models].verify` | string |
| `AGENT_LOOP_MODEL_JUDGE` | `[models].judge` | string |
| `AGENT_LOOP_BUDGET_DAILY_USD` | `[budget].daily_usd` | float |
| `AGENT_LOOP_BUDGET_PER_RUN_USD` | `[budget].per_run_usd` | float |
| `AGENT_LOOP_RUNTIME_SANDBOX` | `[runtime].sandbox` | bool |
| `AGENT_LOOP_RUNTIME_MAX_CYCLES` | `[runtime].max_cycles` | int |
| `AGENT_LOOP_RUNTIME_MAX_REDO` | `[runtime].max_redo` | int |

### Provider credentials

`agent-loop-cli` delegates auth to litellm. Set whichever your phases use:

| Provider | Variable | Used by (default) |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | research / plan / implement / verify |
| OpenAI | `OPENAI_API_KEY` | judge |
| Gemini | `GEMINI_API_KEY` | (any phase if mapped) |
| Azure OpenAI | `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION` | (any phase if mapped via `azure/...`) |
| Local Ollama | (no key) | set models to `ollama/<name>` |
| **Cursor (CLI)** | (no key — `cursor-agent login` once) | set models to `cursor/<id>` |

#### Cursor (CLI)

Models prefixed with `cursor` (e.g. `cursor/auto`, `cursor/sonnet-4`, `cursor/gpt-5`)
delegate to a locally installed [`cursor-agent`](https://cursor.com) CLI in `--print`
mode. cursor-agent is itself an agentic CLI: a single phase call may run tools, edit
files in the workspace, and take 30 s to several minutes.

```bash
cursor-agent login                 # one-time, browser-based
agent-loop doctor                  # confirms PATH + 'Logged in as <email>'
agent-loop test-model cursor/auto  # 'OK' ping (~10-15 s)

# config.toml
[models]
research  = "cursor/auto"
plan      = "cursor/auto"
implement = "cursor/auto"
verify    = "cursor/auto"
judge     = "cursor/auto"
```

`cost_usd` is reported as `0.0` for cursor models (Pro subscription assumed); token
counts are rough char/4 estimates. List candidate model names with `agent-loop models`
or `cursor-agent --list-models`.

### Multi-model setup (cross-vendor judge)

The default config already mixes Anthropic for the build phases with OpenAI for the judge — a
"different family scores work" pattern that catches issues a same-family judge would miss.
Other useful presets:

```toml
# Cheap iteration, expensive arbitration
[models]
research  = "anthropic/claude-haiku-4-5"
plan      = "anthropic/claude-haiku-4-5"
implement = "anthropic/claude-sonnet-4-6"
verify    = "openai/gpt-4.1"
judge     = "anthropic/claude-opus-4-7"

# Local + cloud hybrid (Ollama for cheap phases)
[models]
research  = "ollama/llama3.1:70b"
plan      = "ollama/llama3.1:70b"
implement = "anthropic/claude-sonnet-4-6"
verify    = "ollama/llama3.1:70b"
judge     = "openai/gpt-5.2"

# All-cursor preset: single login, full agentic phases
# (Pro subscription, no per-token metering)
[models]
research  = "cursor/auto"
plan      = "cursor/auto"
implement = "cursor/auto"
verify    = "cursor/auto"
judge     = "cursor/auto"
```

## State directory layout

Every task gets its own directory under `--root` (default `./.agent_loop/`):

```
.agent_loop/<task-id>/
├── task.md                       # Task description (free-form prose)
├── memory.txt                    # v0.1 legacy single-file memory
├── memory.txt.v0_1.bak           # v0.2 migration backup (only when migrating)
├── memory/                       # v0.2 3-tier memory (Context Engine)
│   ├── history.jsonl             # Append-only audit trail (one JSON per phase)
│   ├── episodic.md               # Compactor output, per-cycle one-liners
│   └── core_facts.md             # Persistent patterns (CORE: lines + migrated v0.1)
├── workspace/                    # Phase I sandbox (where solution.py lives)
│   ├── solution.py               # Latest implementation
│   └── best_solution.py          # Snapshot of the best one so far
├── checkpoints/
│   └── cycle_001_phase_implement.json
├── artifacts/
│   ├── findings.md               # R output
│   ├── plan.md                   # P output
│   ├── execution_log.md          # I output
│   ├── solution.json             # V output (rubric scores)
│   ├── best_solution.json        # Promoted by Judge on improvement
│   └── judge_result.json         # J output (better/action/scores)
└── telemetry/
    └── metrics.jsonl             # Per-phase tokens / cost / latency + `_cycle_quality`
```

`metrics.jsonl` is append-only; one JSON object per phase per cycle. Easy to grep, slice with
`jq`, or feed into your own dashboards. Starting in v0.2 each cycle also emits a
`_cycle_quality` row with the Context Engine's sensor metrics
(`duplicate_ratio`, `contradiction_count`, `staleness_age_cycles`, `relevance_score`).

## Context Engine (v0.2)

The Context Engine replaces v0.1's single `memory.txt` with a 3-tier layout, plus
sensors that score the prompt-context quality and a rule-based compactor that runs
once per cycle.

```
memory/
├── history.jsonl     # raw audit, append-only (one record per phase)
├── episodic.md       # per-cycle one-liners + best-score markers (rebuilt by Compactor)
└── core_facts.md     # persistent patterns; lines starting with `CORE:` accumulate here
```

Phase prompts receive `# Episodic\n... \n\n# Core Facts\n...` as the `{memory}`
slot. The Compactor and sensors run after every cycle (in the orchestrator,
right after promote / rollback) and emit a `_cycle_quality` row to
`metrics.jsonl`:

```jsonc
{"phase": "_cycle_quality", "cycle": 2,
 "quality": {"duplicate_ratio": 0.05, "contradiction_count": 0,
             "staleness_age_cycles": 1, "relevance_score": 0.84},
 "compact": {"size_before": 940, "size_after": 980, "lines_kept": 12, ...}}
```

Backward compat: an existing v0.1 task with `memory.txt` is migrated *once* —
its content is copied into `core_facts.md`, the original is renamed to
`memory.txt.v0_1.bak`, and `memory.txt` is left empty so v0.1 readers don't
double-count. Resume works on both v0.1 and v0.2 task directories without a
flag.

The v0.2 Compactor and `contradiction_count` sensor are intentionally rule-based
and LLM-free; v0.3 swaps in optional LLM-backed implementations behind the same
`ContextEngine` interface.

## Benchmarks

Four reference tasks live in `benchmarks/`:

| File | Category | Difficulty | Why |
|---|---|---|---|
| `binary_search.yaml` | search | easy | Quick smoke test, edge cases (duplicates, empty, large) |
| `n_queens.yaml` | algorithm | hard | Plugin parity, hardest perf target (N=13 in <=1.5s) |
| `sort_tuning.yaml` | algorithm | medium | Performance comparison vs builtin |
| `palindrome.yaml` | string | medium | Different domain (strings) |

```bash
python3 -m agent_loop.cli bench                  # run all four
python3 -m agent_loop.cli bench binary_search    # single
python3 -m agent_loop.cli bench --quick          # binary_search only
python3 -m agent_loop.cli bench --dry-run        # parse yaml + write task.md, no LLM
python3 -m agent_loop.cli bench n_queens \
    --cycles 3 --max-redo 1                      # tighter budget overrides
```

Each yaml declares its own `budget.max_cycles` / `max_redo` / `max_usd`; CLI flags override.

## Migration from `agent-loop-plugin`

| | `agent-loop-plugin` (Skill) | `agent-loop-cli` (this repo) |
|---|---|---|
| Runtime | Claude Code only (slash command) | Standalone Python CLI |
| Vendor | Anthropic only | litellm (Anthropic / OpenAI / Gemini / Azure / Ollama) |
| Phase model assignment | Single (the host's model) | Per-phase via `config.toml` |
| State | Skill-managed | `.agent_loop/<task-id>/` (file-based) |
| Resume | Built-in to Claude Code session | `agent-loop resume <id>` |
| Memory | `memory.txt` (single file) | Same — direct port |
| Cycle semantics | R -> P -> I -> V -> J + rollback | **Identical**, prompts are direct ports |
| Distribution | Claude Code Skill marketplace | `pip install` |

If you have a working `agent-loop-plugin` setup, your `task.md` / `memory.txt` carry over verbatim —
drop them into `.agent_loop/<task-id>/` and `python3 -m agent_loop.cli resume <task-id>`.

## Troubleshooting

### "OSError: [Errno 122] Disk quota exceeded" during `pip install`

Most often a *file count* (inode) limit, not bytes. Two fixes:

1. **Bypass the entry point**: don't reinstall, just call the CLI module directly
   `python3 -m agent_loop.cli ...`. This works as long as the `src/` editable install
   from a previous run is intact.
2. **Free inodes**, then reinstall: `pip cache purge`, remove stale `~/.local/lib/python*/site-packages/<pkg>` directories you no longer use, then `pip install -e . --no-deps --user`.

### `module load python/3.12.4` does not change `python3 --version` in a subshell

KISTI's `module load` only affects the **current shell** PATH. If a script forks a subshell, you
must re-load inside it. The Bash recipe used by this repo is

```bash
module load python/3.12.4 >/dev/null 2>&1 && python3 -m agent_loop.cli ...
```

The `python/3.12.4` module emits a deprecation warning and silently redirects to 3.14.2; both
satisfy `requires-python = ">=3.10"`.

### `KeyError` when a prompt template is rendered

The phase prompts in `src/agent_loop/prompts/*.md` are rendered with Python's `str.format`.
Any literal `{` or `}` in a prompt body **must be doubled** (`{{` / `}}`) — this is most common in
JSON examples inside a prompt:

```markdown
Return JSON of the form
{{ "better": true, "action": "stop", "scores": {{ "this_cycle": 0.95 }} }}
```

The five built-in prompts already follow this rule; only relevant if you fork them.

### "task.md is empty" on `resume`

`resume` reads the original `task.md` to re-feed the loop. If you `rm -rf .agent_loop/<id>/task.md`
or never started it via `run`, recreate `task.md` manually before resuming.

## Roadmap

- **v0.1.1** — `cursor-agent` CLI added as a second provider next to litellm,
  plus `agent-loop doctor` and `agent-loop test-model` for environment sanity checks.
- **v0.2 (current, partial)** — Context Engine: 3-tier memory + rule-based Compactor +
  sensor metrics in `metrics.jsonl`. v0.1 `memory.txt` migrates automatically. Multi-axis
  Verify rubric is the next milestone (separate worker).
- **v0.3** — LLM-backed Compactor, multi-judge consensus, multi-strategy parallel
  planning, model-router cost optimization.
- **v0.4** — MCP server mode, cross-task memory, external sensors / tool plugins.

See `docs/plan-v0.1.md` section 4 for the full scope ladder.

## License

MIT — same as `agent-loop-plugin`.

## Related

- [`agent-loop-plugin`](https://github.com/dbscjf0000-web/agent-loop-plugin) — Claude Code Skill
  version, single-vendor, in-host orchestration.
- **PIAMDA v15/v16** — bash-based predecessor on KISTI Neuron, narrower scope (simulation
  optimization).
- `docs/architecture.md` — diagrammed component view, including v0.2+ extraction points.
