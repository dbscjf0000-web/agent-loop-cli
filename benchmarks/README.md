# Benchmarks

Reference tasks used to validate `agent-loop-cli` end-to-end. Each `*.yaml` defines:

- `task` — natural-language description fed to the agent
- `success_criteria` — multi-axis rubric (correctness, performance, complexity, ...)
- `budget` — max cycles / redo / USD cap

## Tasks

| File | Category | Difficulty | Why |
|---|---|---|---|
| `n_queens.yaml` | algorithm | hard (perf goal) | Plugin parity, hardest perf target |
| `binary_search.yaml` | search | easy | Quick smoke test, edge cases |
| `sort_tuning.yaml` | algorithm | medium | Performance comparison vs builtin |
| `palindrome.yaml` | string | medium | Different domain (strings) |

## Run all (planned, after v0.1 lands)

```bash
agent-loop bench                  # run all
agent-loop bench n-queens         # single task
agent-loop bench --quick          # only easy tasks (binary_search)
```

## Adding a benchmark

1. Create `benchmarks/<name>.yaml` (follow same schema)
2. Add row to the table above
3. (Optional) Add reference solution in `benchmarks/_reference/<name>.py`

## Quantitative comparison (`compare.py`)

`compare.py` runs each task twice — once via a single `cursor-agent` call
(`baseline_runner.py`) and once via the full R→P→I→V→J cycle — and scores
both with the same `VerifyEngine` so the result is apples-to-apples.

```bash
module load python/3.12.4

# Pin every loop phase to cursor so both sides hit the same provider.
cat > /tmp/al_compare_config.toml <<'EOF'
[models]
research  = "cursor/auto"
plan      = "cursor/auto"
implement = "cursor/auto"
verify    = "cursor/auto"
judge     = "cursor/auto"

[runtime]
cli_timeout = 240
EOF

python3 benchmarks/compare.py \
    --tasks binary_search,n_queens,palindrome,sort_tuning \
    --runs 2 \
    --output /tmp/al_compare.csv \
    --loop-config /tmp/al_compare_config.toml \
    --baseline-timeout 90 \
    --loop-timeout 300
```

### Multi-cycle mode (v0.5.2)

By default `compare.py` pins the agent-loop side to `--cycles 1` so the
comparison isolates the *loop structure* from the value of *iteration*. To
measure what extra cycles + Judge feedback actually buy, use the v0.5.2 flags:

```bash
python3 benchmarks/compare.py \
    --tasks binary_search,n_queens,palindrome,sort_tuning \
    --runs 2 \
    --output /tmp/al_compare3.csv \
    --loop-config /tmp/al_compare3_config.toml \
    --loop-cycles 3 \
    --loop-max-redo 2 \
    --loop-judge-always-llm \
    --baseline-timeout 90 \
    --loop-timeout 600
```

New flags:

- `--loop-cycles N` — max R→P→I→V→J cycles (default 1).
- `--loop-max-redo N` — max consecutive non-improving cycles before stop (default 1).
- `--loop-judge-always-llm` — force LLM judge call even on cycle 1 (otherwise
  judge short-circuits when there is no prior best). The matching config
  should also set `judge_always_llm = true` in `[runtime]`.

Outputs:

- `<output>` — long-form CSV, one row per (task, method, run_id).
- `<output>.summary.json` — per-task mean / stddev / min / max / pass-rate.
- Console — Markdown-style summary table.

Failed runs (timeouts, missing fenced block, evaluator crash) are kept in
the statistics with `weighted_score = 0.0`. See the README's
"Quantitative comparison" section for the headline numbers (cycles=1 vs
cycles=3 three-way table).
