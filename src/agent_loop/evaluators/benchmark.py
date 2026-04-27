"""Wall-clock benchmark evaluator.

Spec keys:
    weight    (float, required)
    setup     (str, optional) executed once in the namespace before the
              repeats (e.g. import statements, building inputs). Defaults
              to ``"from solution import *"``.
    stmt      (str, required) statement to time, e.g. ``"n_queens_count(13)"``.
    threshold (float, required) target wall-clock time in seconds.
    repeats   (int, optional, default 3) median-of-N runs.
    timeout   (float, optional, default 60) per-run cap; on timeout the run
              counts as ``timeout`` seconds for scoring.
    measure   (str, optional) one of:
              - ``wall_clock_seconds`` (default): score = 1.0 if median <=
                threshold, then linearly drops to 0 at 2 * threshold.
              - ``speedup_ratio``: ``threshold`` is interpreted as the
                target ratio (e.g. 0.9 = "must finish in <= 0.9 * baseline").
                A ``baseline_stmt`` key (e.g. ``"sorted(arr)"``) is required;
                otherwise we fall back to wall_clock_seconds semantics.

The evaluator is single-process (subprocess would help isolate, but in v0.2
we keep it simple — programmatic ground truth without sandboxing).
"""
from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

from agent_loop.config import Config
from agent_loop.evaluators.pytest_runner import _load_solution
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore


def _time_stmt(stmt: str, ns: dict[str, Any], timeout: float) -> float:
    """Run ``stmt`` once, return elapsed seconds. On timeout return ``timeout``."""
    code = compile(stmt, "<benchmark>", "exec")
    started = time.perf_counter()
    try:
        exec(code, ns)
    except Exception as exc:  # propagate as worst-case time + exception in evidence
        elapsed = time.perf_counter() - started
        ns["__benchmark_exception"] = f"{type(exc).__name__}: {exc}"
        return max(elapsed, timeout)
    elapsed = time.perf_counter() - started
    if elapsed > timeout:
        return timeout
    return elapsed


def run_benchmark(
    *,
    name: str,
    spec: dict[str, Any],
    task_dir: TaskDir,
    config: Config,
) -> AxisScore:
    weight = float(spec.get("weight", 1.0) or 0.0)
    stmt = spec.get("stmt")
    threshold = spec.get("threshold")
    if not stmt or threshold is None:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="benchmark",
            evidence="benchmark spec missing 'stmt' or 'threshold'",
            is_ground_truth=True,
        )

    repeats = int(spec.get("repeats", 3) or 3)
    timeout = float(spec.get("timeout", 60) or 60)
    measure = str(spec.get("measure", "wall_clock_seconds") or "wall_clock_seconds")
    setup = spec.get("setup") or "from solution import *"
    baseline_stmt = spec.get("baseline_stmt")

    try:
        mod = _load_solution(task_dir)
    except Exception as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="benchmark",
            evidence=f"import failed: {type(exc).__name__}: {exc}",
            is_ground_truth=True,
        )

    ns: dict[str, Any] = {"solution": mod}
    for attr in dir(mod):
        if not attr.startswith("_"):
            ns[attr] = getattr(mod, attr)
    try:
        exec(compile(setup, "<setup>", "exec"), ns)
    except Exception as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="benchmark",
            evidence=f"setup failed: {type(exc).__name__}: {exc}",
            is_ground_truth=True,
        )

    times = [_time_stmt(stmt, ns, timeout) for _ in range(max(1, repeats))]
    median = statistics.median(times)
    raw: dict[str, Any] = {
        "times_s": [round(t, 6) for t in times],
        "median_s": round(median, 6),
        "threshold": threshold,
        "measure": measure,
    }
    exc = ns.get("__benchmark_exception")
    if exc:
        raw["exception"] = exc

    if measure == "speedup_ratio" and baseline_stmt:
        baseline_times = [_time_stmt(baseline_stmt, ns, timeout) for _ in range(max(1, repeats))]
        baseline_median = statistics.median(baseline_times) or 1e-9
        ratio = median / baseline_median
        raw["baseline_times_s"] = [round(t, 6) for t in baseline_times]
        raw["baseline_median_s"] = round(baseline_median, 6)
        raw["ratio"] = round(ratio, 6)
        target_ratio = float(threshold)
        if ratio <= target_ratio:
            score = 1.0
        elif ratio >= 2 * target_ratio:
            score = 0.0
        else:
            score = 1.0 - (ratio - target_ratio) / target_ratio
        evidence = f"ratio={ratio:.3f}, target<={target_ratio:.3f}"
    else:
        target = float(threshold)
        if median <= target:
            score = 1.0
        elif median >= 2 * target:
            score = 0.0
        else:
            score = 1.0 - (median - target) / target
        evidence = f"median={median:.3f}s, threshold<={target:.3f}s"

    if exc:
        evidence += f" (raised {exc})"
        score = min(score, 0.0)  # exception -> 0 even if timing was fast pre-raise

    return AxisScore(
        name=name,
        score=max(0.0, min(1.0, score)),
        weight=weight,
        evaluator="benchmark",
        evidence=evidence,
        is_ground_truth=True,
        raw=raw,
    )


__all__ = ["run_benchmark"]
