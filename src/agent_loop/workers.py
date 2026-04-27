"""Phase worker functions: R, P, I, V, J.

Each worker:
  1. Reads required artifacts from the TaskDir.
  2. Loads + formats the prompt template under prompts/.
  3. Calls call_model() for its phase.
  4. Persists outputs back to the TaskDir.
  5. Returns the raw ModelResponse so the orchestrator can record metrics.

No abstract base classes. Just five plain functions plus small helpers.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from agent_loop.config import Config
from agent_loop.models import ModelResponse, call_model
from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a focused, sober software engineer. Follow instructions exactly. "
    "Prefer correctness over cleverness. Output only what the prompt asks for."
)


def _load_prompt(name: str) -> str:
    """Load prompts/<name>.md from the installed package."""
    pkg = "agent_loop.prompts"
    try:
        return resources.files(pkg).joinpath(f"{name}.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback for editable installs that don't expose prompts as a sub-package.
        here = Path(__file__).parent / "prompts" / f"{name}.md"
        return here.read_text(encoding="utf-8")


_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_python(text: str) -> tuple[str, str]:
    """Return (code, prose).

    Pulls the first ```python ... ``` block out as `code`. Everything else is
    the prose (with the fenced block removed). If no fence is found, returns
    ("", text).
    """
    m = _FENCE_RE.search(text)
    if not m:
        return "", text.strip()
    code = m.group(1).rstrip() + "\n"
    prose = (text[: m.start()] + text[m.end() :]).strip()
    return code, prose


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from an LLM response.

    Tries (in order):
      1. Direct json.loads of the trimmed text.
      2. The first ```json ... ``` (or generic ``` ... ```) fenced block.
      3. The substring from the first '{' to the last '}'.

    Raises ValueError if all attempts fail.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty response")

    # 1. Direct parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2. Fenced block
    m = _JSON_FENCE_RE.search(s)
    if m:
        block = m.group(1).strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    # 3. Brace slice
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"could not parse JSON from response (len={len(s)})")


def _read_or(task_dir: TaskDir, name: str, default: str = "") -> str:
    if not task_dir.has_artifact(name):
        return default
    val = task_dir.read_artifact(name)
    return val if isinstance(val, str) else json.dumps(val, indent=2, ensure_ascii=False)


def _workspace_listing(task_dir: TaskDir) -> str:
    ws = task_dir.workspace_path()
    if not ws.exists():
        return "(no workspace)"
    lines: list[str] = []
    for p in sorted(ws.rglob("*")):
        if p.is_file():
            lines.append(f"{p.relative_to(ws)}\t{p.stat().st_size}B")
    return "\n".join(lines) if lines else "(empty)"


def _import_check(task_dir: TaskDir, *, timeout: float = 5.0) -> str:
    """Run `python -c 'import solution'` against workspace/solution.py.

    Returns a short human-readable report. Used to give the verifier a sanity
    signal before it scores correctness.
    """
    sol = task_dir.workspace_path() / "solution.py"
    if not sol.exists():
        return "FAIL: workspace/solution.py does not exist"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import solution; print('OK', sorted(k for k in dir(solution) if not k.startswith('_')))",
            ],
            cwd=str(task_dir.workspace_path()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "FAIL: import timed out"
    if proc.returncode == 0:
        return f"OK\nstdout: {proc.stdout.strip()}"
    return f"FAIL (rc={proc.returncode})\nstderr: {proc.stderr.strip()[:1500]}"


# ---------------------------------------------------------------------------
# phase functions
# ---------------------------------------------------------------------------


def run_research(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = task_dir.memory_md_path().read_text(encoding="utf-8")
    prompt = _load_prompt("research").format(task=task, memory=memory or "(none)")
    resp = call_model(
        "research",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )
    task_dir.write_artifact("findings.md", resp.text)
    return resp


def run_plan(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = task_dir.memory_md_path().read_text(encoding="utf-8")
    findings = _read_or(task_dir, "findings.md", "(no findings)")
    prompt = _load_prompt("plan").format(
        task=task, memory=memory or "(none)", findings=findings
    )
    resp = call_model(
        "plan",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )
    task_dir.write_artifact("plan.md", resp.text)
    return resp


def run_implement(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    plan = _read_or(task_dir, "plan.md", "(no plan)")
    best_summary = "(none)"
    if task_dir.has_artifact("best_solution.json"):
        best = task_dir.read_artifact("best_solution.json")
        if isinstance(best, dict):
            best_summary = json.dumps(
                {
                    "weighted_score": best.get("weighted_score"),
                    "axes": best.get("axes"),
                    "evidence": best.get("evidence"),
                },
                indent=2,
                ensure_ascii=False,
            )
    prompt = _load_prompt("implement").format(
        task=task, plan=plan, best_solution_summary=best_summary
    )
    ws = task_dir.workspace_path()
    ws.mkdir(parents=True, exist_ok=True)
    resp = call_model(
        "implement",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=ws,
    )

    code, prose = _extract_python(resp.text)
    if code:
        (ws / "solution.py").write_text(code, encoding="utf-8")
    task_dir.write_artifact("execution_log.md", prose or resp.text)
    return resp


def run_verify(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    plan = _read_or(task_dir, "plan.md", "(no plan)")
    exec_log = _read_or(task_dir, "execution_log.md", "(no log)")
    listing = _workspace_listing(task_dir)
    sol_path = task_dir.workspace_path() / "solution.py"
    solution_code = (
        sol_path.read_text(encoding="utf-8") if sol_path.exists() else "(no solution.py)"
    )
    import_check = _import_check(task_dir)

    prompt = _load_prompt("verify").format(
        task=task,
        plan=plan,
        execution_log=exec_log,
        workspace_listing=listing,
        solution_code=solution_code[:8000],  # cap context
        import_check=import_check,
    )
    resp = call_model(
        "verify",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )

    try:
        parsed = _extract_json(resp.text)
    except ValueError:
        parsed = {
            "axes": {"correctness": 0.0, "performance": 0.0, "robustness": 0.0, "code_quality": 0.0},
            "weighted_score": 0.0,
            "evidence": "verifier produced unparseable JSON",
            "issues": ["unparseable verifier output"],
            "_raw": resp.text[:2000],
        }
    task_dir.write_artifact("solution.json", parsed)
    return resp


def run_judge(task_dir: TaskDir, config: Config) -> ModelResponse:
    """Compare current solution.json to best_solution.json and decide next action.

    Special case: if no best_solution exists yet (first cycle), short-circuit
    to better=true without spending an LLM call.
    """
    if not task_dir.has_artifact("best_solution.json"):
        # First cycle: nothing to compare against.
        sol = task_dir.read_artifact("solution.json") if task_dir.has_artifact("solution.json") else {}
        score = sol.get("weighted_score", 0.0) if isinstance(sol, dict) else 0.0
        result = {
            "better": True,
            "action": "stop" if score >= 0.95 else "redo_R",
            "reason": "no prior best — first cycle is automatically the best",
            "hint": "next cycle should iterate on weak axes" if score < 0.95 else "",
            "scores": {"this_cycle": score, "best": None, "delta": None},
        }
        task_dir.write_artifact("judge_result.json", result)
        return ModelResponse(
            text=json.dumps(result),
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_s=0.0,
            model="(skipped: first cycle)",
        )

    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = task_dir.memory_md_path().read_text(encoding="utf-8")
    sol_obj = task_dir.read_artifact("solution.json") if task_dir.has_artifact("solution.json") else {}
    best_obj = task_dir.read_artifact("best_solution.json")
    sol_str = json.dumps(sol_obj, indent=2, ensure_ascii=False)
    best_str = json.dumps(best_obj, indent=2, ensure_ascii=False)

    max_redo = config.runtime.max_redo
    redo_count = _current_redo_count(task_dir)

    prompt = _load_prompt("judge").format(
        task=task,
        solution=sol_str,
        best_solution=best_str,
        memory=memory or "(none)",
        redo_count=redo_count,
        max_redo=max_redo,
    )
    resp = call_model(
        "judge",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )

    try:
        parsed = _extract_json(resp.text)
    except ValueError:
        parsed = {
            "better": False,
            "action": "stop",
            "reason": "judge produced unparseable JSON; halting to be safe",
            "hint": "",
            "scores": {},
            "_raw": resp.text[:2000],
        }
    task_dir.write_artifact("judge_result.json", parsed)
    return resp


def _current_redo_count(task_dir: TaskDir) -> int:
    """Pull redo_count from the latest checkpoint (default 0)."""
    cp = task_dir.load_latest_checkpoint()
    if not cp:
        return 0
    return int(cp.get("payload", {}).get("redo_count", 0))
