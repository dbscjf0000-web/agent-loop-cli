# agent-loop-cli

> Standalone CLI for the R->P->I->V->J agent loop. Multi-model. File-based state. Resume-friendly.

`agent-loop-cli` runs a 5-stage Research -> Plan -> Implement -> Verify -> Judge cycle against any
LLM (Claude / GPT / Gemini / local), with regression-proof rollback and resumable checkpoints.

## Status

**v0.4.0** — cross-task global memory. ContextEngine now snapshots a slice of
`~/.agent-loop/global/patterns.md` and the orchestrator commits this task's
`CORE:` lines + a one-line summary at run end. Two tasks under the same user
share patterns automatically; `agent-loop memory {show,list,wipe,path}` manages
the dir. Privacy: only `CORE:` lines and `task_md_first_line` ever leave the
task dir — code, prompts, and full task text stay local. `--no-cross-task`
disables for a single run; `runtime.cross_task_memory = false` disables
permanently. 151 unit tests passing (132 v0.3.2 + 19 cross-task). MCP server
exposing the global dir is queued for v0.4.1.

- **v0.3-dev** — multi-judge consensus engine (worker-B) and multi-strategy plan
  fan-out (worker-C) both landed. `JudgeEngine` runs N parallel judges with
  weighted-majority aggregation; `StrategyEngine` runs N parallel plans through
  a heuristic+LLM Selector and writes the winner to `plan.md`. CLI surfaces both
  via `--judge` / `--strategy` (each repeatable) plus `AGENT_LOOP_RUNTIME_JUDGES`
  / `AGENT_LOOP_RUNTIME_STRATEGIES` env vars.

- **v0.2.1** — three CLI providers (`cursor-agent`, Claude Code `claude`, Google `gemini`) plus
  litellm in one model dispatch table, 77 unit tests passing, and a cross-vendor live
  `bench binary_search` run on KISTI Neuron with the v0.2 Verify Engine and Judge short-circuit
  (weighted_score=1.000, 1 cycle stop, total ~77 s wall clock).

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

#### Free-form follow-up (2026-04-27): proving the LLM path actually executes

To remove the rubric-short-circuit caveat above, two free-form `agent-loop run`
sessions were driven on KISTI Neuron with `--cycles 2`. Without a YAML rubric,
the Verify Engine cannot ground-truth and must call the LLM verifier.

**Session 1 — `cursor + gemini-2.5-flash` (verify & judge), task `gcd(a, b)`**
(`task_id=13fb98`):

| phase     | latency | model                    | LLM ran?               |
|-----------|---------|--------------------------|------------------------|
| research  |  22.75s | cursor/auto              | yes (293/265 tokens)   |
| plan      |  22.20s | cursor/auto              | yes (539/308 tokens)   |
| implement |  15.77s | cursor/auto              | yes (633/210 tokens)   |
| verify    |  15.25s | gemini/gemini-2.5-flash  | **yes (873/129 tokens)**, Korean evidence in `solution.json` |
| judge     |   0.00s | (skipped: first cycle)   | no — `"no prior best — first cycle is automatically the best"` |

`weighted_score=1.000`, `cycles_run=1`, `final_status=stop`. So with `cursor` driving the
generative phases and `gemini-2.5-flash` doing free-form Verify, the cross-vendor
LLM path is *proven to execute* — what was previously the most fragile claim of v0.2.1
(was Verify just rubric short-circuiting?) is resolved on the verify side.

**Session 2 — `cursor + claude/default verify + gemini judge`, task `is_palindrome(s)`**
(`task_id=db7f56`): research/plan/implement completed via cursor (52.1s combined),
then `claude --print` was invoked for free-form Verify and timed out:

```
RuntimeError: claude CLI timed out after 600s (model=default,
workspace=/tmp/al_xv2/.agent_loop/db7f56/workspace)
```

This reproduces the warning above: `claude --print` on a free-form Verify prompt
behaves agentically and can exceed the default 600 s `cli_timeout`. Run wall clock
was 656 s (cursor 52 s + claude 600 s timeout + cleanup). No code changes were
attempted in this run; the timeout is recorded as-is so users picking
`verify = "claude/default"` know to either (a) raise `cli_timeout`, (b) switch verify
to `gemini-2.5-flash`, or (c) prefer rubric-anchored Verify when claude is the verifier.

**Resolved caveat:** the v0.2.1 cross-vendor run never proved the LLM Verify path
worked across vendors because the rubric short-circuited it. Session 1 closes that gap
for the cursor->gemini direction with a real, multi-call free-form trace.

**Remaining limitations:**

1. **Judge skipped on cycle 1.** `judge_result.json` reports `"no prior best —
   first cycle is automatically the best"` and writes `latency_s=0.0` with
   `model="(skipped: first cycle)"`. So the cross-vendor *judge* leg is still
   not proven for tasks that hit weighted_score=1.0 on cycle 1; you need a
   harder task that fails verify on cycle 1 (or a multi-cycle policy that
   forces the judge LLM regardless) to exercise it.
2. **`claude/default` verify timeout** on free-form Verify is real, not a
   transient — bump `cli_timeout` or pick a smaller verifier.

#### Multi-judge + multi-strategy live verify (v0.3, 2026-04-27)

Closes the "judge skipped on cycle 1" gap above by enabling
`runtime.judge_always_llm = true`, which disables the first-cycle short-circuit
and forces a real LLM judge invocation even when there is no prior `best_solution`
to compare against. Both v0.3 features are exercised in the same run on a tiny
free-form task `reverse_words(s)` with **3 plan strategies** + **3 consensus judges**
across all three vendors (cursor, gemini-flash, claude). `task_id=9380cc`.

```toml
# agent-loop.toml — abridged
[runtime]
judge_always_llm = true
cli_timeout         = 600
cli_timeout_verify  = 1200

[[runtime.strategies]]
provider = "cursor/auto"
weight = 1.0
[[runtime.strategies]]
provider = "gemini/gemini-2.5-flash"
weight = 1.0
[[runtime.strategies]]
provider = "claude/default"
weight = 1.0

[[runtime.judges]]
provider = "cursor/auto"
weight = 1.0
[[runtime.judges]]
provider = "gemini/gemini-2.5-flash"
weight = 1.0
[[runtime.judges]]
provider = "claude/default"
weight = 1.0
```

**Multi-strategy result** (`artifacts/proposals.json` + `artifacts/plan_selector.json`):

| # | provider                | error                  | text len | structural | llm score | final |
|---|-------------------------|------------------------|----------|------------|-----------|-------|
| 0 | cursor/auto             | none                   | 1465     | 0.458      | 0.96      | 0.759 |
| 1 | gemini/gemini-2.5-flash | none                   | 2232     | 0.582      | 0.88      | **0.7607 (winner)** |
| 2 | claude/default          | `claude CLI timed out` | 0        | 0.000      | n/a       | 0.000 |

`selector_method = "heuristic+llm"` — the v0.3.0 LLM-anchored selector picked
`gemini-2.5-flash` (final=0.7607) over cursor (0.759) by a 0.0017 margin, and the
selector LLM correctly justified it ("Proposal #2 has no usable plan"). `plan.md`
on disk is **byte-identical** to `proposals[winner_index].text`. Telemetry row:

```json
{"phase": "plan", "n_strategies": 3, "selector_method": "heuristic+llm",
 "winner_index": 1, "winner_provider": "gemini/gemini-2.5-flash",
 "latency_s": 89.256, "model": "(strategy: gemini/gemini-2.5-flash of 3)"}
```

**Multi-judge result** (`artifacts/judge_result.json`):

| # | provider                | better | action | weighted_score | latency  | error                  |
|---|-------------------------|--------|--------|----------------|----------|------------------------|
| 0 | cursor/auto             | true   | stop   | 1.000          |   13.1 s | none                   |
| 1 | gemini/gemini-2.5-flash | true   | stop   | 1.000          |   33.8 s | none                   |
| 2 | claude/default          | false  | stop   | null           |  600.1 s | `claude CLI timed out` |

Consensus: `n_judges=3`, `votes_action={"stop": 2.0}`, `votes_better={"true": 2.0,
"false": 0.0}`, `fallback=false`. Two healthy vendors agreed on `stop`/`better=true`
(claude excluded by error); `consensus.individual` keeps all three for audit.
Telemetry:

```json
{"phase": "judge", "n_judges": 3, "votes_action": {"stop": 2.0},
 "votes_better": {"true": 2.0, "false": 0.0}, "consensus_fallback": false,
 "latency_s": 33.79, "model": "(consensus: 3 judges)"}
```

**`judge_always_llm` proof.** With this flag off (default), cycle 1 short-circuits
without any LLM call (`latency_s=0.0`, `model="(skipped: first cycle)"`). With it on,
every healthy judge actually ran an LLM call on cycle 1 — both `cursor/auto`
(13.1 s) and `gemini/gemini-2.5-flash` (33.8 s) returned non-zero latency and
real `reason` text in their `individual` entries, even though `best_solution.json`
did not exist when the judges started. This was previously listed as a remaining
limitation; v0.3.1 closes it.

**Run stats:** total wall clock ~22 min (sum of phase latencies = 259.2 s / 4.3 min
of real work, but each phase that includes `claude/default` waits its full 600 s
timeout on the slowest leg — once in plan strategies, once in judge consensus).
Per-phase: research 22.4 s (cursor), plan 89.3 s (3-strategy parallel, capped by
the timed-out claude leg), implement 14.5 s (cursor), verify 99.3 s
(gemini-flash, free-form), judge 33.8 s (3-judge parallel, also waiting on the
600 s claude timeout in the background). Cycles=1, cost=$0 (cursor Pro +
gemini-flash free tier); `final_status=stop`, `weighted_score=1.0`. The hot path
stayed responsive thanks to `ThreadPoolExecutor` fan-out: cursor and gemini-flash
finished plan in 22 / 89 s and judge in 13 / 34 s, while claude blocked the full
600 s in the background of each phase.

**Remaining limitation found:** `claude --print` (the `claude/default` provider)
times out at 600 s on plan and judge prompts as well — not just verify.
For multi-vendor consensus runs that include claude, set a higher
`cli_timeout` or expect to lose the claude leg to error (consensus still works
on the surviving 2 vendors with `fallback=false`).

#### v0.3.2 patch verified (2026-04-27): claude tool-block dodges self-invoke timeout

The "claude/default 600 s timeout on free-form Verify/judge prompts" limitation
above is **resolved**. Root cause: `claude --print` is itself agentic and on
long prompts the model decides to recursively self-invoke its own tools (Read,
Bash, etc.), blowing past the timeout even on `--dangerously-skip-permissions`.

**Patch.** `_call_claude_cli` now blocks all tools via a phantom name and pins
the workspace to the flag with the equals form (so `nargs='*'` on
`--allowedTools` cannot swallow the workspace path):

```python
cmd = [
    binary, "--print",
    "--output-format", "text",
    "--dangerously-skip-permissions",
    "--allowedTools=NoneSuch",     # phantom tool blocks self-invoke
    f"--add-dir={workspace}",      # equals form keeps it bound
    rendered_prompt,
]
```

**Live cross-vendor re-verification** (`task_id=54c997`, free-form
`is_palindrome(s: str)`, `cursor` × 3 + `claude/default` verify +
`gemini-2.5-flash` judge with `judge_always_llm = true`):

| phase     | latency  | model                    | LLM ran?               |
|-----------|----------|--------------------------|------------------------|
| research  |  24.36 s | cursor/auto              | yes (293/336 tokens)   |
| plan      |  17.88 s | cursor/auto              | yes (611/403 tokens)   |
| implement |  12.61 s | cursor/auto              | yes (727/160 tokens)   |
| verify    | **14.61 s** | **claude/default**       | **yes (920/175 tokens)** — analytic evidence: *"two-pointer implementation… Time O(n/2), space O(1)"* |
| judge     |  48.08 s | gemini/gemini-2.5-flash  | yes (515/66 tokens)    — `judge_always_llm` cycle-1 LLM call |
| **total** | **~2 min** |                        |                        |

`weighted_score=0.975`, `final_status=stop`, `cycles_run=1`. The claude verify
leg returned in 14.6 s with substantive code analysis — proving the tool-block
patch lets free-form claude verify actually run end-to-end on cycle 1, with
`judge_always_llm` forcing a real gemini judge LLM call on top.

**Before / after**:

| scenario                          | before patch | after patch (v0.3.2) |
|-----------------------------------|--------------|----------------------|
| `claude --print` short ping       | 10.3 s       | **7.1 s**            |
| `claude/default` free-form verify | 600 s timeout (`RuntimeError`) | **14.6 s** |
| `claude/default` plan/judge prompts in multi-vendor consensus | 600 s timeout (graceful degrade to surviving 2) | expected ≤ 60 s (untested in this run, but same patch path) |

The fix unblocks `verify = "claude/default"` for free-form `agent-loop run`
without raising `cli_timeout` or swapping verifiers.

## Multi-judge consensus (v0.3)

The single LLM judge of v0.1/v0.2 can be replaced with **N parallel judges + weighted-majority
consensus**. Cross-vendor is the point: same-vendor fan-out is no signal.

```toml
# agent-loop.toml — three judges, equal weight
[[judges]]
provider = "claude/default"
weight = 1.0

[[judges]]
provider = "gemini/gemini-2.5-flash"
weight = 1.0

[[judges]]
provider = "cursor/auto"
weight = 1.0
```

Or the short form (all weight=1.0):

```toml
[runtime]
judges = ["claude/default", "gemini/gemini-2.5-flash", "cursor/auto"]
```

Or per-run via the CLI (overrides config):

```bash
agent-loop run "..." \
  --judge claude/default \
  --judge gemini/gemini-2.5-flash \
  --judge cursor/auto
```

Or via env var (comma-separated, weight=1.0 each):

```bash
export AGENT_LOOP_RUNTIME_JUDGES="claude/default,gemini/gemini-2.5-flash,cursor/auto"
agent-loop bench n_queens --cycles 2
```

### How consensus is computed

| Field | Rule | Tie-break |
|---|---|---|
| `action` | weighted majority on `stop` / `redo_R` / `redo_P` | `stop` preferred (conservative); else alphabetic first |
| `better` | weighted true vs. false sum | `False` (conservative — don't promote unless clearly better) |
| `scores.weighted` | weighted average across judges that reported a score | `None` if no judge gave one |
| `hint` / `reason` | `\n---\n` concat (with `[provider]` tag on `reason`) | n/a |

Each judge runs **in parallel** via `concurrent.futures.ThreadPoolExecutor` (CLI subprocess
calls are IO-bound, so threads are fine — no new dependencies). Wall-clock latency is the
**max** across judges, not the sum.

### Output schema

`artifacts/judge_result.json` (multi mode) gains a `consensus` block alongside the canonical
fields. Single mode (no `runtime.judges`) is unchanged for backward compatibility.

```jsonc
{
  "better": true,
  "action": "stop",
  "scores": {"weighted": 0.85, "this_cycle": 0.85, "best": 0.78, "delta": 0.07},
  "hint": "...\n---\n...",
  "reason": "[claude/default] ...\n---\n[gemini/...] ...",
  "consensus": {
    "n_judges": 3,
    "votes_action": {"stop": 2.0, "redo_P": 1.0},
    "votes_better": {"true": 2.0, "false": 1.0},
    "fallback": false,
    "individual": [
      {"provider": "claude/default", "weight": 1.0, "better": true, "action": "stop",
       "weighted_score": 0.88, "hint": "...", "reason": "...", "error": null,
       "latency_s": 4.21},
      // ...
    ]
  }
}
```

### Failure handling

- **One judge fails** (timeout / parse error): recorded as `individual[i].error`. Consensus
  proceeds with the rest (partial consensus).
- **All judges fail**: silent fallback to a single-judge call (`config.models.judge`). The
  resulting `judge_result.json` is annotated `consensus.fallback = true` so observers can
  see what happened.
- **First cycle** (no `best_solution.json` yet): the multi-judge path defers to the single
  short-circuit — no fan-out cost on cycle 1.

### Caveats

- Recommended N ≤ 3 to stay under inode / load pressure on shared clusters.
- `gemini/gemini-2.5-flash` is the fastest judge in our setup (~8 s cold, ~3 s warm) and is
  the default suggestion for one of the three slots.
- The slowest judge sets the critical path. If `claude/default` warms up at 60 s, the
  whole consensus waits.

## Multi-strategy plan (v0.3)

The single LLM plan call of v0.1/v0.2 can be replaced with **N parallel plan
proposals + a Selector that picks the best one**. As with multi-judge, cross-vendor
is the point — same-vendor fan-out gives no diversity signal.

```toml
# agent-loop.toml — three plan strategies, equal selector tie-break weight
[[strategies]]
provider = "claude/default"
weight = 1.0

[[strategies]]
provider = "gemini/gemini-2.5-flash"
weight = 1.0

[[strategies]]
provider = "cursor/auto"
weight = 1.0
```

Or short form (all weight=1.0):

```toml
[runtime]
strategies = ["claude/default", "gemini/gemini-2.5-flash", "cursor/auto"]
```

Or per-run via the CLI (overrides config):

```bash
agent-loop run "..." \
  --strategy claude/default \
  --strategy gemini/gemini-2.5-flash \
  --strategy cursor/auto
```

Or via env var (comma-separated, weight=1.0 each):

```bash
export AGENT_LOOP_RUNTIME_STRATEGIES="claude/default,gemini/gemini-2.5-flash,cursor/auto"
agent-loop bench binary_search --cycles 1
```

### How the Selector picks a winner (v0.3.0)

1. **Structural score** (LLM-free): length (clamp 200..4000 chars), code-fence presence,
   numbered-step count (log-scaled), header count (log-scaled). Weighted sum lands in
   [0, 1].
2. **LLM rubric** (one extra `cfg.models.plan` call): asks the planning model to rank
   every proposal in `[0, 1]`. JSON output `{winner_index, reason, scores: list[float]}`.
3. **Final score** = `0.6 * llm + 0.4 * structural` when the LLM call succeeded;
   structural-only otherwise (`selector_method = "fallback"`).
4. **Tie-break**: higher `StrategySpec.weight` wins; if still tied, the lower input
   index wins (deterministic).

If only one strategy is configured the selector is skipped entirely (`selector_method
= "single"`, no LLM cost).

### Output schema

`artifacts/plan.md` is the **winner's text verbatim**, so every downstream phase
(Implement / Verify / Judge) is unaware of the fan-out. Two new audit artifacts:

`artifacts/proposals.json`:

```jsonc
{
  "proposals": [
    {"provider": "claude/default", "weight": 1.0, "text": "...", "cost_usd": 0.0,
     "latency_s": 4.2, "tokens_in": 0, "tokens_out": 0, "error": null},
    // ...
  ]
}
```

`artifacts/plan_selector.json`:

```jsonc
{
  "winner_index": 0,
  "winner_provider": "claude/default",
  "selector_method": "heuristic+llm",
  "selector_error": null,
  "selector_reason": "more concrete steps + benchmark threshold called out",
  "scores": [
    {"provider": "claude/default", "structural": 0.82, "llm": 0.91, "final": 0.876, "error": null},
    {"provider": "cursor/auto",   "structural": 0.74, "llm": 0.70, "final": 0.716, "error": null}
  ]
}
```

### Failure handling

- **One strategy fails** (timeout / parse error): recorded as `proposal[i].error`. The
  Selector runs over the remaining valid proposals.
- **All strategies fail**: `AllStrategiesFailed` is raised; the orchestrator treats it
  as an explicit cycle error (no silent fallback). Use a single `[runtime].plan` model
  if you want graceful degradation.
- **Selector LLM fails**: structural-only fallback (`selector_method = "fallback"`,
  `selector_error` populated).
- **Single proposal**: selector skipped, winner is that proposal.

### Caveats

- Recommended N ≤ 3 (same shared-cluster constraints as multi-judge).
- The LLM rubric uses your *plan* model — if you set that to a slow CLI like
  `claude/default`, expect the rubric to add ~10 s on top of the parallel critical path.
- `selector_method == "fallback"` is captured on the plan metric row in
  `metrics.jsonl` so observers can detect rubric outages.

## Cross-task memory (v0.4)

> v0.4.2 fix: `--no-cross-task` is now honored inside phase prompts as well
> (previously workers built a default `ContextEngine` that ignored the flag).

Each task directory keeps its own `memory/core_facts.md` (3-tier Context Engine,
v0.2). v0.4 promotes any line that starts with `CORE:` up into a per-user global
directory at `~/.agent-loop/global/`, so future tasks see prior learning:

```
~/.agent-loop/global/
├── patterns.md         # CORE: lines from all tasks (deduplicated)
└── task_index.jsonl    # one row per completed task (audit trail)
```

The orchestrator calls `ContextEngine.commit_to_global(...)` at the end of every
run (`stop` / `max_redo` / `max_cycles` / `budget_exceeded` — every exit path),
appending only:

- new `CORE:` lines from this task's `core_facts.md` (exact-match dedup against
  the existing `patterns.md`)
- one row in `task_index.jsonl`: `{task_id, weighted_score, cycles, final_status,
  task_md_first_line, timestamp}`

`MemorySnapshot.render()` adds a `# Global Patterns (cross-task)` section to
the `{memory}` slot of every phase prompt when the file is non-empty.

### Privacy

- Only `CORE:` lines (which you opt into via judge hints) and the **first line**
  of `task.md` ever leave the task directory.
- Code (`solution.py`), `plan.md`, full task descriptions, and LLM responses are
  never copied to the global dir.
- `agent-loop memory wipe` deletes the entire global dir after confirmation.
- Single-host only (no cloud sync). `runtime.cross_task_memory = false` reverts
  to v0.3 single-task behaviour.

### CLI

```bash
agent-loop memory path                    # print ~/.agent-loop/global/
agent-loop memory show --limit 50         # print last 50 patterns
agent-loop memory list                    # rich table of past tasks
agent-loop memory wipe [--yes]            # delete the dir (confirms unless --yes)

agent-loop run "..." --no-cross-task      # disable for this run only
```

### Config

```toml
[runtime]
cross_task_memory               = true              # default ON
cross_task_memory_dir           = "~/.agent-loop/global"
cross_task_memory_max_chars     = 4000              # snapshot slice budget
```

Environment overrides:

- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY=false`
- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR=/path/to/dir`
- `AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_MAX_CHARS=8000`

### Caveats

- Recommended ceiling: ~50 KB in `patterns.md` before signal-to-noise drops.
  `cross_task_memory_max_chars` (default 4000) caps how much enters the prompt
  on each phase; trailing slice (most recent commits win).
- Concurrent runs share the same `patterns.md` — append-only `O_APPEND` writes
  + dedup-on-read make worst-case race a duplicate line, never corruption.
- `commit_to_global` is **idempotent** for the same `task_id`; `agent-loop resume`
  does not double-count.

## Auto-rubric (v0.4.1)

> v0.4.2: cost / tokens / latency for the auto-rubric LLM call are now tracked
> as a dedicated `phase=_auto_rubric` row in `telemetry/metrics.jsonl` (and
> rolled into the per-run budget), instead of being silently dropped.

`agent-loop run "<task>"` (free-form prose, no benchmark YAML) used to fall
back to a single-shot LLM verifier — one axis, one number, one paragraph of
evidence. v0.4.1 closes that gap: at the end of the **Research** phase the
LLM proposes a multi-axis rubric, persisted as `artifacts/rubric_auto.json`,
which the Verify Engine then drives just like a hand-written / yaml-derived
rubric.

```bash
agent-loop run "Implement gcd(a, b) for integers."

# Cycle 1 / Research writes both:
#   artifacts/findings.md
#   artifacts/rubric_auto.json     <- NEW
```

Sample `rubric_auto.json`:

```json
{
  "axes": {
    "correctness": {
      "weight": 0.5,
      "evaluator": "llm_rubric",
      "criterion": "function returns the correct gcd for typical inputs"
    },
    "edge_cases": {
      "weight": 0.3,
      "evaluator": "llm_rubric",
      "criterion": "handles zero and negative inputs gracefully"
    },
    "code_quality": {
      "weight": 0.2,
      "evaluator": "llm_rubric",
      "criterion": "uses Euclidean algorithm idiomatically"
    }
  }
}
```

Verify priority order:

1. `artifacts/rubric.json` — hand-written or yaml-derived (`bench`). Always wins.
2. `artifacts/rubric_auto.json` — Research-generated (free-form `run`). Used when `runtime.auto_rubric=true` (default).
3. Legacy single-shot LLM verifier — fallback when neither rubric is present.

`solution.json` ends up with the same `axes` *list* + `weighted_score` + `summary`
schema as a yaml-driven bench, so the Judge phase sees a richer signal across
cycles.

### Disabling

```bash
# One-shot opt-out
agent-loop run "..." --no-auto-rubric

# Permanent: agent-loop.toml
[runtime]
auto_rubric = false

# Or env
export AGENT_LOOP_RUNTIME_AUTO_RUBRIC=false
```

`bench` is unaffected — yaml-driven `rubric.json` always wins.

### Caveats

- The rubric is generated from `task.md` + `findings.md` only — no code is read,
  so all axes are `evaluator: "llm_rubric"`. (Code-aware pytest/benchmark axis
  generation is the v0.4.2 candidate.)
- Each verify call costs N×LLM rubric calls (one per axis). Default rubric
  has 3-5 axes, so verify takes ~3-5× the legacy single-shot cost.
- LLMs are non-deterministic — re-running the same task can produce a slightly
  different rubric. The rubric lives in `artifacts/rubric_auto.json`; you can
  edit it before re-running the loop.

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

# v0.3.1 — CLI subprocess timeout (seconds). `cli_timeout` is the default
# applied to every phase. Per-phase overrides win when set.
cli_timeout         = 600
cli_timeout_verify  = 900   # claude --print verify can saturate at 600 s
cli_timeout_judge   = 180

# v0.3.1 — disable the judge first-cycle short-circuit. Required for genuine
# multi-judge cross-vendor verification when verify_score>=0.95 on cycle 1
# (otherwise the judge auto-stops without ever invoking the LLM).
judge_always_llm    = false
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
| `AGENT_LOOP_RUNTIME_JUDGES` | `[runtime].judges` (v0.3) | comma-separated providers (weight=1.0 each) |
| `AGENT_LOOP_RUNTIME_STRATEGIES` | `[runtime].strategies` (v0.3) | comma-separated providers (weight=1.0 each) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT` | `[runtime].cli_timeout` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT_RESEARCH` | `[runtime].cli_timeout_research` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT_PLAN` | `[runtime].cli_timeout_plan` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT_IMPLEMENT` | `[runtime].cli_timeout_implement` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT_VERIFY` | `[runtime].cli_timeout_verify` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_CLI_TIMEOUT_JUDGE` | `[runtime].cli_timeout_judge` (v0.3.1) | int (seconds) |
| `AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM` | `[runtime].judge_always_llm` (v0.3.1) | bool |

#### v0.3.1 CLI flags

`agent-loop run` and `agent-loop bench` accept the following overrides
(applied on top of file/env config):

| Flag | Maps to | Notes |
|---|---|---|
| `--cli-timeout <int>` | `runtime.cli_timeout` | default for every phase |
| `--cli-timeout-verify <int>` | `runtime.cli_timeout_verify` | per-phase, wins over default |
| `--cli-timeout-judge <int>` | `runtime.cli_timeout_judge` | per-phase, wins over default |
| `--judge-always-llm` | `runtime.judge_always_llm` | flag, no value |

Example — relax verify timeout to 900 s and force the judge to run on
cycle 1 (multi-judge cross-vendor verification):

```bash
agent-loop run "is_palindrome free-form" \
  --cli-timeout-verify 900 \
  --judge-always-llm \
  --judge claude/default --judge gemini/gemini-2.5-flash
```

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
