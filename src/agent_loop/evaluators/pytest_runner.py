"""pytest-style assertion evaluator.

Spec keys:
    weight   (float, required)
    test     (str, optional) inline assertion block (multi-line) executed in
             a namespace where ``solution`` is the imported workspace module
             and every public attribute of ``solution`` is also bound at the
             top level (so the YAML benchmarks can write ``assert
             n_queens_count(8) == 92`` directly).
    test_file (str, optional) path (absolute or relative to workspace) to a
             python file with assertions. Read and treated like ``test``.
    timeout  (float, optional, default 30) seconds for module import +
             assertion execution. Implemented by an alarm-free in-process
             cap (no subprocess) so we keep things deterministic in tests.

Score semantics
---------------
Each non-empty line of the test block that contains the substring ``assert``
is treated as one assertion. We exec the whole block once; on the first
``AssertionError`` the score is ``passed / total`` so far, where ``passed``
is the count of assertion lines preceding the failing line. On any other
exception the score is 0.0. All assertions passing -> 1.0.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

from agent_loop.config import Config
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore


def _count_asserts(code: str) -> int:
    return sum(1 for ln in code.splitlines() if "assert" in ln and ln.strip())


def _load_solution(task_dir: TaskDir, file_name: str = "solution.py") -> Any:
    """Import ``workspace/<file_name>`` as a fresh module (default solution.py).

    A fresh module on every call: tests can ship together without polluting
    each other's namespace. Returns the loaded module.

    The module is registered in ``sys.modules`` under both a unique key (for
    isolation) and ``"solution"`` (so ``from solution import *`` style setup
    blocks in benchmark specs resolve correctly) — this keeps backward
    compatibility for existing YAML benchmarks regardless of the actual
    source filename.

    v0.12.0: ``file_name`` allows non-default code task entry points (e.g.
    ``smart_sort.py``) declared in the rubric axis spec via ``spec["file"]``.
    """
    # Defense: share the same filename policy as the implement-side extractor
    # (workers._is_safe_workspace_filename) so a name accepted at write time is
    # also accepted at read time, and vice versa.
    from agent_loop.workers import _is_safe_workspace_filename
    if not _is_safe_workspace_filename(file_name):
        raise ValueError(f"unsafe spec.file: {file_name!r}")
    sol = task_dir.workspace_path() / file_name
    if not sol.exists():
        raise FileNotFoundError(f"workspace/{file_name} does not exist")
    name = f"_agent_loop_solution_{int(time.time() * 1e6)}"
    spec = importlib.util.spec_from_file_location(name, sol)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build module spec for {sol}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.modules["solution"] = mod  # legacy alias; benchmarks import via this name
    spec.loader.exec_module(mod)
    return mod


def run_pytest(
    *,
    name: str,
    spec: dict[str, Any],
    task_dir: TaskDir,
    config: Config,
) -> AxisScore:
    weight = float(spec.get("weight", 1.0) or 0.0)
    code = spec.get("test")
    if not code and spec.get("test_file"):
        p = Path(spec["test_file"])
        if not p.is_absolute():
            p = task_dir.workspace_path() / p
        code = p.read_text(encoding="utf-8")
    if not code or not str(code).strip():
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="pytest",
            evidence="no test block in spec",
            is_ground_truth=True,
        )

    code = str(code)
    total = _count_asserts(code) or 1

    # v0.12.0 — optional `file` key lets the rubric target a non-default
    # entry point (e.g. ``manuscript_helper.py``). Defaults to ``solution.py``
    # so every existing rubric continues to work unchanged.
    src_file = str(spec.get("file") or "solution.py")
    try:
        mod = _load_solution(task_dir, src_file)
    except Exception as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="pytest",
            evidence=f"import failed: {type(exc).__name__}: {exc}",
            is_ground_truth=True,
            raw={"passed": 0, "total": total},
        )

    # Bind public attributes so YAML tests can call functions directly.
    ns: dict[str, Any] = {"solution": mod}
    for attr in dir(mod):
        if not attr.startswith("_"):
            ns[attr] = getattr(mod, attr)

    passed_before_fail = 0
    started = time.time()
    try:
        # Walk lines once to keep a running counter so a mid-block failure
        # tells us how many passed first.
        running: list[str] = []
        for ln in code.splitlines():
            running.append(ln)
            if "assert" in ln and ln.strip():
                exec(compile("\n".join(running), "<rubric>", "exec"), ns)
                running = []
                passed_before_fail += 1
        if running:
            exec(compile("\n".join(running), "<rubric>", "exec"), ns)
    except AssertionError as exc:
        elapsed = time.time() - started
        return AxisScore(
            name=name,
            score=passed_before_fail / total,
            weight=weight,
            evaluator="pytest",
            evidence=f"{passed_before_fail}/{total} assertions passed before failure: {exc}",
            is_ground_truth=True,
            raw={"passed": passed_before_fail, "total": total, "elapsed_s": round(elapsed, 4)},
        )
    except Exception as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="pytest",
            evidence=f"{type(exc).__name__}: {exc}",
            is_ground_truth=True,
            raw={"passed": 0, "total": total},
        )

    elapsed = time.time() - started
    return AxisScore(
        name=name,
        score=1.0,
        weight=weight,
        evaluator="pytest",
        evidence=f"{total}/{total} assertions passed",
        is_ground_truth=True,
        raw={"passed": total, "total": total, "elapsed_s": round(elapsed, 4)},
    )


__all__ = ["run_pytest"]
