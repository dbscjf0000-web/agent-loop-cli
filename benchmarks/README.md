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
