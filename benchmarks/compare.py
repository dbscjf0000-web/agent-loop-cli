"""Quantitative head-to-head: cursor single-shot vs agent-loop 5-phase.

Usage (from repo root, with python3.12+):
    module load python/3.12.4
    python3 benchmarks/compare.py \
        --tasks binary_search,n_queens,palindrome,sort_tuning \
        --runs 3 \
        --output /tmp/al_compare.csv

For each (task, method) pair we run ``runs`` independent trials. Both
methods are scored against the same ``yaml_to_rubric``-derived rubric so
the comparison is apples-to-apples.

Failure handling: a baseline subprocess that times out / errors out still
counts as a row with ``weighted_score=0``. An agent-loop run that crashes
also counts as score=0. We never silently drop a run.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

# Make src/ importable when run from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml  # type: ignore

from agent_loop.config import load_config
from agent_loop.state import TaskDir, new_task_id
from agent_loop.verify_engine import VerifyEngine, yaml_to_rubric, result_to_dict

# Local import (sibling file)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from baseline_runner import run_baseline  # noqa: E402


__all__ = ["run_comparison", "summarize", "score_solution"]


# ---------------------------------------------------------------------------
# scoring shim
# ---------------------------------------------------------------------------
def _build_scoring_taskdir(workspace: Path) -> TaskDir:
    """Build a TaskDir whose ``workspace_path()`` returns ``workspace``.

    VerifyEngine.evaluate(...) -> evaluators expect ``task_dir.workspace_path()``
    to point at the dir containing solution.py. We don't need any of the
    other artefacts (memory, telemetry, etc.) for pure scoring, so we just
    point a TaskDir at the parent of ``workspace`` and override the relevant
    paths via a thin subclass.
    """
    workspace = workspace.resolve()
    # Use parent + name so TaskDir.path == workspace.parent / workspace.name
    parent = workspace.parent
    name = workspace.name

    td = TaskDir(root=parent, task_id=name)
    td.init()  # idempotent; creates artifacts/, memory/, etc. *inside* workspace

    # init() created subdirs under <workspace>/artifacts etc. — that's fine.
    # The evaluators only call workspace_path(), which by default is
    # task_dir.path / "workspace". Solution.py lives at workspace/solution.py
    # which equals td.path/solution.py, NOT td.path/workspace/solution.py.
    #
    # Patch: drop a symlink (or copy) of solution.py into the expected
    # subdir so the evaluators find it without any monkey-patching.
    workspace_subdir = td.path / "workspace"
    workspace_subdir.mkdir(parents=True, exist_ok=True)
    src = td.path / "solution.py"
    dst = workspace_subdir / "solution.py"
    if src.exists():
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            # use a copy (not symlink) for portability across filesystems
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    return td


def score_solution(
    yaml_path: Path,
    workspace: Path,
    *,
    config: Any | None = None,
) -> dict[str, Any]:
    """Score ``workspace/solution.py`` against ``yaml_path``'s rubric.

    Returns a dict matching VerifyEngine's serialised output:
        {"weighted_score", "summary", "axes": [...]}.

    On any internal failure returns ``weighted_score=0`` plus an error
    string in ``summary``.
    """
    try:
        spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "weighted_score": 0.0,
            "summary": f"yaml parse failed: {type(exc).__name__}: {exc}",
            "axes": [],
        }
    crit = (spec or {}).get("success_criteria") or []
    rubric = yaml_to_rubric(crit)
    if not rubric.get("axes"):
        return {
            "weighted_score": 0.0,
            "summary": "yaml has no success_criteria",
            "axes": [],
        }

    cfg = config if config is not None else load_config(None)
    td = _build_scoring_taskdir(workspace)
    engine = VerifyEngine(td, cfg)
    try:
        result = engine.evaluate(rubric, llm_fallback=False)
    except Exception as exc:
        return {
            "weighted_score": 0.0,
            "summary": f"verify crashed: {type(exc).__name__}: {exc}",
            "axes": [],
        }
    return result_to_dict(result)


# ---------------------------------------------------------------------------
# baseline run wrapper
# ---------------------------------------------------------------------------
def run_baseline_once(
    yaml_path: Path,
    *,
    run_id: int,
    timeout: int = 180,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    task_text = (spec or {}).get("task") or ""
    if workspace_root is None:
        workspace_root = Path(tempfile.mkdtemp(prefix="al_baseline_"))
    workspace = workspace_root / f"baseline_{yaml_path.stem}_{run_id}_{new_task_id()}"
    workspace.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    bres = run_baseline(task_text=task_text, workspace=workspace, timeout=timeout)
    sres = score_solution(yaml_path, workspace)
    total = time.monotonic() - started

    return {
        "method": "baseline",
        "task": yaml_path.stem,
        "run_id": run_id,
        "weighted_score": float(sres.get("weighted_score") or 0.0),
        "summary": sres.get("summary") or "",
        "axes": sres.get("axes") or [],
        "latency_s": float(bres.get("latency_s") or 0.0),
        "total_s": round(total, 3),
        "extracted": bool(bres.get("extracted")),
        "returncode": int(bres.get("returncode") or 0),
        "timed_out": bool(bres.get("timed_out")),
        "workspace": str(workspace),
    }


# ---------------------------------------------------------------------------
# agent-loop run wrapper
# ---------------------------------------------------------------------------
def run_agent_loop_once(
    yaml_path: Path,
    *,
    run_id: int,
    timeout: int = 600,
    root_dir: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Invoke ``python -m agent_loop.cli bench <name>`` and harvest the score.

    The bench command writes a ``solution.json`` artifact with the
    ``weighted_score`` we want, alongside a ``solution.py`` we re-score
    via VerifyEngine for parity with baseline.
    """
    name = yaml_path.stem
    if root_dir is None:
        root_dir = Path(tempfile.mkdtemp(prefix="al_loop_"))
    root_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "agent_loop.cli",
        "bench",
        name,
        "--root",
        str(root_dir),
        "--cycles",
        "1",  # one full R/P/I/V/J cycle for fairness with baseline (one shot)
    ]
    if config_path:
        cmd.extend(["--config", str(config_path)])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC) + ":" + env.get("PYTHONPATH", "")

    started = time.monotonic()
    rc = 0
    stderr = ""
    stdout = ""
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = -1
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    latency = time.monotonic() - started

    # find the bench task dir (newest under root_dir matching `bench-<name>-*`)
    task_dirs = sorted(
        [p for p in root_dir.glob(f"bench-{name}-*") if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    score: dict[str, Any] = {"weighted_score": 0.0, "summary": "", "axes": []}
    workspace = ""
    if task_dirs:
        task_dir = task_dirs[-1]
        workspace = str(task_dir / "workspace")
        # Re-score from solution.py using the same rubric path so baseline +
        # agent-loop go through identical evaluators.
        sol_py = task_dir / "workspace" / "solution.py"
        if sol_py.exists():
            score = score_solution(yaml_path, task_dir / "workspace")
        else:
            # fall back to the in-tree solution.json the bench wrote
            sj = task_dir / "artifacts" / "solution.json"
            if sj.exists():
                try:
                    score = json.loads(sj.read_text(encoding="utf-8"))
                except Exception:
                    pass

    return {
        "method": "agent_loop",
        "task": name,
        "run_id": run_id,
        "weighted_score": float(score.get("weighted_score") or 0.0),
        "summary": (score.get("summary") or "")[:200],
        "axes": score.get("axes") or [],
        "latency_s": round(latency, 3),
        "total_s": round(latency, 3),
        "extracted": bool(workspace),
        "returncode": rc,
        "timed_out": timed_out,
        "workspace": workspace,
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def run_comparison(
    yaml_path: Path,
    *,
    runs: int = 3,
    baseline_timeout: int = 180,
    loop_timeout: int = 600,
    loop_config: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    rows = {"baseline": [], "agent_loop": []}
    for i in range(1, runs + 1):
        try:
            r = run_baseline_once(yaml_path, run_id=i, timeout=baseline_timeout)
        except Exception as exc:
            r = {
                "method": "baseline",
                "task": yaml_path.stem,
                "run_id": i,
                "weighted_score": 0.0,
                "summary": f"crashed: {type(exc).__name__}: {exc}",
                "axes": [],
                "latency_s": 0.0,
                "total_s": 0.0,
                "extracted": False,
                "returncode": -3,
                "timed_out": False,
                "workspace": "",
            }
        rows["baseline"].append(r)
        print(
            f"  baseline #{i}: score={r['weighted_score']:.3f} "
            f"latency={r['latency_s']:.1f}s "
            f"summary={(r['summary'] or '')[:80]}"
        )

    for i in range(1, runs + 1):
        try:
            r = run_agent_loop_once(
                yaml_path, run_id=i, timeout=loop_timeout, config_path=loop_config
            )
        except Exception as exc:
            r = {
                "method": "agent_loop",
                "task": yaml_path.stem,
                "run_id": i,
                "weighted_score": 0.0,
                "summary": f"crashed: {type(exc).__name__}: {exc}",
                "axes": [],
                "latency_s": 0.0,
                "total_s": 0.0,
                "extracted": False,
                "returncode": -3,
                "timed_out": False,
                "workspace": "",
            }
        rows["agent_loop"].append(r)
        print(
            f"  agent_loop #{i}: score={r['weighted_score']:.3f} "
            f"latency={r['latency_s']:.1f}s "
            f"summary={(r['summary'] or '')[:80]}"
        )
    return rows


def summarize(rows: list[dict[str, Any]], *, pass_threshold: float = 0.95) -> dict[str, float]:
    if not rows:
        return {"n": 0, "mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0,
                "mean_latency": 0.0, "pass_rate": 0.0}
    scores = [float(r["weighted_score"]) for r in rows]
    lats = [float(r["latency_s"]) for r in rows]
    return {
        "n": len(rows),
        "mean": round(statistics.mean(scores), 4),
        "stddev": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "mean_latency": round(statistics.mean(lats), 2),
        "pass_rate": round(sum(1 for s in scores if s >= pass_threshold) / len(scores), 3),
    }


# ---------------------------------------------------------------------------
# CSV / table output
# ---------------------------------------------------------------------------
def _axes_csv(axes: list[dict[str, Any]]) -> str:
    if not axes:
        return ""
    bits = []
    for a in axes:
        try:
            bits.append(f"{a.get('name', '?')}={float(a.get('score', 0)):.2f}")
        except Exception:
            bits.append(str(a.get("name") or "?"))
    return ";".join(bits)


def write_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "task", "method", "run_id", "weighted_score", "latency_s",
            "extracted", "returncode", "timed_out", "axes", "summary",
        ])
        for r in rows:
            w.writerow([
                r["task"], r["method"], r["run_id"],
                f"{float(r['weighted_score']):.4f}",
                f"{float(r['latency_s']):.2f}",
                int(bool(r["extracted"])),
                r["returncode"], int(bool(r["timed_out"])),
                _axes_csv(r.get("axes") or []),
                (r.get("summary") or "").replace("\n", " ")[:200],
            ])


def print_summary_table(per_task: dict[str, dict[str, dict[str, float]]]) -> None:
    print()
    print("=" * 90)
    print(f"{'Task':<16} | {'Baseline μ±σ (latency)':<32} | {'Agent-loop μ±σ (latency)':<32} | Δscore  Δtime")
    print("-" * 90)
    for task, sm in per_task.items():
        b = sm.get("baseline") or {}
        a = sm.get("agent_loop") or {}
        bs = f"{b.get('mean', 0):.2f} ± {b.get('stddev', 0):.2f} ({b.get('mean_latency', 0):.0f}s)"
        as_ = f"{a.get('mean', 0):.2f} ± {a.get('stddev', 0):.2f} ({a.get('mean_latency', 0):.0f}s)"
        ds = a.get("mean", 0) - b.get("mean", 0)
        bl = b.get("mean_latency") or 0.0
        al = a.get("mean_latency") or 0.0
        dt_pct = (al - bl) / bl * 100 if bl else 0.0
        print(f"{task:<16} | {bs:<32} | {as_:<32} | {ds:+.2f}  {dt_pct:+.0f}%")
    print("=" * 90)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="cursor baseline vs agent-loop comparison")
    ap.add_argument("--tasks", default="binary_search,n_queens,palindrome,sort_tuning",
                    help="comma-separated yaml stems under benchmarks/")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--output", default="/tmp/al_compare.csv")
    ap.add_argument("--baseline-timeout", type=int, default=180)
    ap.add_argument("--loop-timeout", type=int, default=600)
    ap.add_argument("--bench-dir", default=str(_REPO_ROOT / "benchmarks"))
    ap.add_argument(
        "--loop-config",
        default=None,
        help="agent-loop config TOML to use (defaults to ~/.agent-loop/config.toml). "
             "For cross-vendor comparison pin all phases to one provider via this flag.",
    )
    args = ap.parse_args()
    loop_cfg = Path(args.loop_config) if args.loop_config else None

    bench_dir = Path(args.bench_dir)
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]

    all_rows: list[dict[str, Any]] = []
    per_task: dict[str, dict[str, dict[str, float]]] = {}

    overall_start = time.monotonic()
    for t in tasks:
        yaml_path = bench_dir / f"{t}.yaml"
        if not yaml_path.exists():
            print(f"[skip] no yaml at {yaml_path}")
            continue
        print(f"\n=== task: {t} ===")
        rows = run_comparison(
            yaml_path,
            runs=args.runs,
            baseline_timeout=args.baseline_timeout,
            loop_timeout=args.loop_timeout,
            loop_config=loop_cfg,
        )
        all_rows.extend(rows["baseline"] + rows["agent_loop"])
        per_task[t] = {
            "baseline": summarize(rows["baseline"]),
            "agent_loop": summarize(rows["agent_loop"]),
        }
        # incremental CSV save so a crash mid-run still leaves data
        write_csv(all_rows, Path(args.output))
        print(f"  → CSV {args.output} (rows so far: {len(all_rows)})")

    print_summary_table(per_task)
    print(f"\nTotal wall: {time.monotonic() - overall_start:.0f}s")
    print(f"CSV: {args.output}")
    # also dump per-task summary JSON next to CSV
    json_out = Path(args.output).with_suffix(".summary.json")
    json_out.write_text(json.dumps(per_task, indent=2), encoding="utf-8")
    print(f"Summary JSON: {json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
