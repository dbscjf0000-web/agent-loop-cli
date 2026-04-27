# agent-loop-cli

> Standalone CLI for the R->P->I->V->J agent loop. Multi-model. File-based state. Resume-friendly.

`agent-loop-cli` runs a 5-stage Research -> Plan -> Implement -> Verify -> Judge cycle against any
LLM (Claude / GPT / Gemini / local), with regression-proof rollback and resumable checkpoints.

## Status

**v0.2.1** — three CLI providers (`cursor-agent`, Claude Code `claude`, Google `gemini`) plus
litellm in one model dispatch table, 77 unit tests passing, and a cross-vendor live
`bench binary_search` run on KISTI Neuron with the v0.2 Verify Engine and Judge short-circuit
(weighted_score=1.000, 1 cycle stop, total ~77 s wall clock). Earlier milestones:

- **v0.2** — Context Engine (3-tier memory) + Verify Engine (rubric-driven multi-axis scoring)
  live-validated on `n_queens` (cursor/auto, weighted_score=1.000).
- **v0.1.1** — `cursor-agent` provider integration, first live e2e (binary_search, score 0.973).
- **v0.1.0** — feature-complete MVP, dry-run e2e for all four reference benchmarks.

Litellm-backed runs (Anthropic / OpenAI / Gemini / Azure / Ollama) still depend on provider API
keys and are exercised by mocked tests only.

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

### Live e2e (v0.2) — n_queens with multi-axis Verify Engine

The v0.2 live run targets the hardest reference benchmark with a programmatic
rubric (no LLM verifier), demonstrating both the Verify Engine and the
Context Engine end-to-end. All five phases use `cursor/auto`:

```bash
python3 -m agent_loop.cli bench n_queens \
    --config /tmp/al_v02_e2e/config.toml \
    --root /tmp/al_v02_e2e/.agent_loop \
    --cycles 2 --max-redo 1
```

Result on KISTI Neuron (`task_id=bench-n_queens-7fc13f`, 1 cycle, `final_status=stop`):

| phase           | latency | model                    |
|-----------------|---------|--------------------------|
| research        |  26.84s | cursor/auto              |
| plan            |  25.12s | cursor/auto              |
| implement       |  16.09s | cursor/auto              |
| verify          |   4.40s | (verify_engine: rubric)  |
| judge           |   0.00s | (skipped: first cycle)   |
| **total**       | **72.45s** |                       |

`solution.json` (multi-axis schema, both ground-truth):

```jsonc
{"weighted_score": 1.0,
 "summary": "correctness=1.00 performance=1.00 -> 1.000",
 "axes": [
   {"name": "correctness", "score": 1.0, "weight": 0.5,
    "evaluator": "pytest", "evidence": "10/10 assertions passed",
    "is_ground_truth": true,
    "raw": {"passed": 10, "total": 10, "elapsed_s": 1.26}},
   {"name": "performance", "score": 1.0, "weight": 0.5,
    "evaluator": "benchmark",
    "evidence": "median=1.046s, threshold<=1.500s",
    "is_ground_truth": true,
    "raw": {"times_s": [1.043, 1.046, 1.046],
            "median_s": 1.046, "threshold": 1.5,
            "measure": "wall_clock_seconds"}}]}
```

Context Engine layout written by the run:

```
memory/
├── history.jsonl   # 5 records (research / plan / implement / verify / judge)
├── episodic.md     # 5 lines, ★best marker on the verify row
└── core_facts.md   # empty (no CORE: hints emitted this cycle)

telemetry/metrics.jsonl  # 6 rows: 5 phase + 1 _cycle_quality:
   {"phase":"_cycle_quality","cycle":1,
    "quality":{"duplicate_ratio":0.0,"contradiction_count":0,
               "staleness_age_cycles":0,"relevance_score":0.965},
    "compact":{"size_before":0,"size_after":285,
               "lines_kept":5,"core_extracted":0,"triggered":true}}
```

The `solution.py` cursor-agent produced for n_queens used bitmask backtracking
with first-row symmetry — `n_queens_count(13)` measured at **1.046 s median**
(safely under the 1.5 s ground-truth threshold). All ten correctness asserts
(N=1..13) passed in the same `pytest` evaluator run. Judge auto-stopped on
the first cycle because there was no prior best to beat.

### Cross-vendor live e2e (v0.2.1) — three CLI vendors in one run

The v0.2.1 milestone adds two new CLI providers — Claude Code (`claude/<id>`) and
Google Gemini (`gemini/<id>`) — alongside the existing cursor-agent path.
A live `bench binary_search` was driven on KISTI Neuron with **three different
vendors across the five phases**:

```toml
# /tmp/al_3vendor/config.toml
[models]
research  = "cursor/auto"
plan      = "cursor/auto"
implement = "cursor/auto"
verify    = "claude/default"
judge     = "gemini/gemini-2.5-flash"
```

```bash
python3 -m agent_loop.cli bench binary_search \
    --config /tmp/al_3vendor/config.toml \
    --root /tmp/al_3vendor/.agent_loop \
    --cycles 2 --max-redo 1
```

Result (`task_id=bench-binary_search-06627c`, 1 cycle, `final_status=stop`,
`weighted_score=1.000`):

| phase           | latency | model                    |
|-----------------|---------|--------------------------|
| research        |  25.92s | cursor/auto              |
| plan            |  39.56s | cursor/auto              |
| implement       |  11.59s | cursor/auto              |
| verify          |   0.03s | (verify_engine: rubric)  |
| judge           |   0.00s | (skipped: first cycle)   |
| **total**       | **~77 s** |                       |

The ground-truth shortcuts kicked in (rubric short-circuit + first-cycle judge
auto-stop), so neither claude nor gemini ran on this particular cycle —
the v0.2 Verify Engine deliberately bypasses the LLM verifier whenever a
benchmark has `success_criteria`. The provider plumbing was exercised
independently:

```bash
agent-loop test-model cursor/auto             # 11.82 s -> 'OK'
agent-loop test-model claude/default          # 10.27 s -> 'OK'
agent-loop test-model gemini/gemini-2.5-flash #  8.19 s -> 'OK'
```

Use this preset on tasks without a YAML rubric (e.g. `agent-loop run "..."` for a
free-form prompt) to actually drive claude through legacy LLM verify and gemini
through the second-cycle judge. Note that `claude --print` is itself agentic
and can take several minutes per call; bump `cli_timeout` if needed.

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
| Gemini (API) | `GEMINI_API_KEY` | (any phase if mapped) |
| Azure OpenAI | `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION` | (any phase if mapped via `azure/...`) |
| Local Ollama | (no key) | set models to `ollama/<name>` |
| **Cursor (CLI)** | (no key — `cursor-agent login` once) | set models to `cursor/<id>` |
| **Claude Code (CLI)** | (no key — run `claude` once to log in) | set models to `claude/<id>` |
| **Gemini (CLI)** | (no key — run `gemini` once to OAuth) | set models to `gemini/<id>` |

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

#### Claude Code CLI

Models prefixed with `claude` (e.g. `claude/default`) delegate to a locally installed
[`claude`](https://claude.com/code) CLI in `--print` mode. Like cursor-agent it is itself
an agent — a single phase call may run tools and edit files. Authentication uses the
user's existing Claude Code login (no API key needed). The wrapper passes
`--dangerously-skip-permissions` (sandbox-only) and `--add-dir <workspace>`.

```bash
claude                              # one-time, browser-based login
agent-loop doctor                   # confirms PATH + version
agent-loop test-model claude/default   # 'OK' ping (~10 s)
```

#### Gemini CLI

Models prefixed with `gemini` (e.g. `gemini/gemini-2.5-pro`, `gemini/gemini-2.5-flash`)
delegate to the locally installed [`gemini`](https://github.com/google-gemini/gemini-cli)
CLI in `-p` headless mode (`--yolo --skip-trust --include-directories <workspace>`).
Authentication uses the user's `oauth-personal` Google login (Google One AI Pro).
Gemini CLI requires Node v22+.

```bash
gemini                              # one-time, OAuth-personal login
agent-loop doctor                   # confirms PATH + version + node v22+
agent-loop test-model gemini/gemini-2.5-flash   # 'OK' ping (~8 s; pro can be 1+ min cold start)
```

> Note: `gemini-2.5-pro` cold-start can exceed 60 s. Use `flash` for ping/judge and
> bump `cli_timeout` for `pro` if you map it to long-running phases.

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

# All-CLI cross-vendor preset (v0.2.1): every phase ends in a different vendor's
# CLI. Build phases use cursor (fast cycle), verify uses Claude Code, judge uses
# Gemini Flash. No API keys needed — only logged-in CLIs. When a benchmark has
# `success_criteria` in YAML the v0.2 Verify Engine auto-generates `rubric.json`
# and the verify phase short-circuits the LLM call (claude is invoked only on
# tasks without a rubric).
[models]
research  = "cursor/auto"
plan      = "cursor/auto"
implement = "cursor/auto"
verify    = "claude/default"
judge     = "gemini/gemini-2.5-flash"
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
│   ├── rubric.json               # V input (v0.2 multi-axis Verify Engine; optional)
│   ├── solution.json             # V output (axes list + weighted_score in v0.2)
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

## Verify Engine (v0.2)

The Verify phase used to be a single LLM call returning a JSON of axes. v0.2
adds a multi-axis rubric driven by **programmatic ground-truth evaluators**;
LLM rubrics survive only as a soft fallback.

If the task directory contains `artifacts/rubric.json`, `run_verify` calls the
Verify Engine instead of an LLM. Each axis is dispatched to one of four
evaluators:

| Evaluator | Spec keys | Score semantics |
|---|---|---|
| `pytest`     | `weight`, `test` (or `test_file`)     | `passed/total` of `assert` lines; import error -> 0.0 |
| `benchmark`  | `weight`, `stmt`, `threshold`, `repeats?`, `measure?` (`wall_clock_seconds` / `speedup_ratio` + `baseline_stmt`) | 1.0 if median <= threshold, linearly down to 0 at 2*threshold |
| `ast_grep`   | `weight`, `rule` (mini-DSL: `` `tok`_count<=N`` / `` `tok` not_in`` / `` `tok` in``) | starts at 1.0, -0.5 per violated rule (clipped) |
| `llm_rubric` | `weight`, `criterion`                 | LLM returns `{score, evidence}` JSON; **not** ground truth |

`weighted_score = Σ(score * weight) / Σ(weight)`. Programmatic axes are
flagged `is_ground_truth: true` so the Judge / sensors can prefer them
over LLM rubrics.

Example `rubric.json`:

```jsonc
{
  "axes": {
    "correctness": {
      "evaluator": "pytest", "weight": 0.5,
      "test": "assert n_queens_count(8) == 92\nassert n_queens_count(13) == 73712"
    },
    "performance": {
      "evaluator": "benchmark", "weight": 0.3,
      "stmt": "n_queens_count(13)", "threshold": 1.5, "repeats": 3,
      "measure": "wall_clock_seconds"
    },
    "complexity": {
      "evaluator": "ast_grep", "weight": 0.2,
      "rule": "`for `_count<=2; `.index(` not_in"
    }
  }
}
```

Resulting `solution.json` schema:

```jsonc
{
  "weighted_score": 0.85,
  "summary": "correctness=1.00 performance=0.70 complexity=1.00 -> 0.850",
  "axes": [
    {"name": "correctness", "score": 1.0, "weight": 0.5,
     "evaluator": "pytest", "evidence": "10/10 assertions passed",
     "is_ground_truth": true,
     "raw": {"passed": 10, "total": 10, "elapsed_s": 0.014}},
    {"name": "performance", "score": 0.7, "weight": 0.3,
     "evaluator": "benchmark", "evidence": "median=1.78s, threshold<=1.500s",
     "is_ground_truth": true,
     "raw": {"times_s": [1.81, 1.78, 1.77], "median_s": 1.78, "threshold": 1.5}},
    ...
  ]
}
```

`agent-loop bench <name>` automatically converts each benchmark YAML's
`success_criteria` into a rubric via `verify_engine.yaml_to_rubric` and
writes it to `artifacts/rubric.json` before the loop starts. Tasks
without a rubric still run the legacy v0.1 LLM verifier — full backward
compatibility.

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
- **v0.2 (current)** — Context Engine: 3-tier memory + rule-based Compactor + sensor
  metrics in `metrics.jsonl` (v0.1 `memory.txt` migrates automatically). **Multi-axis
  Verify Engine**: rubric-driven `pytest` / `benchmark` / `ast_grep` evaluators with
  `llm_rubric` fallback; benchmarks write `rubric.json` automatically.
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
