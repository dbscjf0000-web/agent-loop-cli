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
import time
from importlib import resources
from pathlib import Path
from typing import Any

from agent_loop.config import Config
from agent_loop.context import ContextEngine
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


def _memory_text(task_dir: TaskDir) -> str:
    """Render the v0.2 ContextEngine snapshot as the ``{memory}`` prompt slot.

    Falls back to the legacy ``memory.txt`` content when the engine has no data
    yet (very first phase of a fresh task) so the prompt never sees a blank
    string when something useful is available.
    """
    eng = ContextEngine(task_dir)
    eng.init()
    snap = eng.snapshot()
    rendered = snap.render()
    if rendered.strip() in ("", "# Episodic\n(none)\n\n# Core Facts\n(none)"):
        return "(none)"
    return rendered


def _summarize(text: str, limit: int = 200) -> str:
    """Tiny summary helper — keep first non-empty line up to ``limit`` chars."""
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return text[:limit]


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


def _current_cycle(task_dir: TaskDir) -> int:
    """Best-effort cycle number for ContextEngine history records.

    Reads the latest checkpoint; missing checkpoint means cycle 1.
    """
    cp = task_dir.load_latest_checkpoint()
    if not cp:
        return 1
    try:
        return int(cp.get("cycle", 1))
    except (TypeError, ValueError):
        return 1


def run_research(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir)
    prompt = _load_prompt("research").format(task=task, memory=memory)
    resp = call_model(
        "research",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )
    task_dir.write_artifact("findings.md", resp.text)
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "research",
            "summary": _summarize(resp.text),
            "model": resp.model,
        }
    )
    return resp


def run_plan(task_dir: TaskDir, config: Config) -> ModelResponse:
    """Generate plan.md.

    Two modes:
      - **single** (default): one LLM plan call as configured by ``config.models.plan``.
      - **multi-strategy** (v0.3): when ``config.runtime.strategies`` is non-empty,
        ``_run_plan_multi`` fans out to N providers in parallel and a Selector
        picks one winner. The winner's text is written to ``plan.md`` so all
        downstream phases (Implement / Verify / Judge) are unaware of the
        fan-out.
    """
    if config.runtime.strategies:
        return _run_plan_multi(task_dir, config)
    return _run_plan_single(task_dir, config)


def _run_plan_single(task_dir: TaskDir, config: Config) -> ModelResponse:
    """v0.1 / v0.2 body: single LLM plan call. Preserved verbatim for backward compat."""
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir)
    findings = _read_or(task_dir, "findings.md", "(no findings)")
    prompt = _load_prompt("plan").format(
        task=task, memory=memory, findings=findings
    )
    resp = call_model(
        "plan",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=task_dir.workspace_path(),
    )
    task_dir.write_artifact("plan.md", resp.text)
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "plan",
            "summary": _summarize(resp.text),
            "model": resp.model,
        }
    )
    return resp


def _run_plan_multi(task_dir: TaskDir, config: Config) -> ModelResponse:
    """v0.3 multi-strategy: N strategies fan out, selector picks winner.

    Writes:
      - artifacts/proposals.json     -- full audit of every PlanProposal
      - artifacts/plan_selector.json -- winner index, scores, selector method
      - artifacts/plan.md            -- winner.text (downstream-compatible)

    On ``AllStrategiesFailed`` the exception propagates so the orchestrator
    treats it as an explicit cycle error (rather than silently falling back
    to single, which would mask a complete cross-vendor outage).
    """
    # Late import: keeps workers.py importable even if strategy_engine errors.
    from agent_loop.strategy_engine import (
        AllStrategiesFailed,
        StrategyEngine,
        proposal_to_dict,
        selection_to_dict,
    )

    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir)
    findings = _read_or(task_dir, "findings.md", "(no findings)")
    prompt = _load_prompt("plan").format(
        task=task, memory=memory, findings=findings
    )

    engine = StrategyEngine(task_dir, config)
    started = time.time()

    selection = engine.fanout(config.runtime.strategies, prompt)

    # Write audit artifacts.
    proposals_payload = {
        "proposals": [proposal_to_dict(p) for p in selection.proposals],
    }
    task_dir.write_artifact("proposals.json", proposals_payload)
    task_dir.write_artifact("plan_selector.json", selection_to_dict(selection))

    # Winner text becomes the canonical plan.md so downstream phases don't change.
    task_dir.write_artifact("plan.md", selection.winner.text)

    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "plan",
            "summary": _summarize(selection.winner.text),
            "model": f"(strategy: {selection.winner.provider})",
            "n_proposals": len(selection.proposals),
            "selected_provider": selection.winner.provider,
            "selector_method": selection.selector_method,
        }
    )

    # Aggregate ModelResponse: cost = sum, latency = max (parallel critical path)
    total_cost = sum(p.cost_usd for p in selection.proposals if p.error is None)
    latencies = [p.latency_s for p in selection.proposals if p.error is None] or [0.0]
    tokens_in = sum(p.tokens_in for p in selection.proposals if p.error is None)
    tokens_out = sum(p.tokens_out for p in selection.proposals if p.error is None)
    return ModelResponse(
        text=selection.winner.text,
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
        cost_usd=round(total_cost, 6),
        latency_s=round(max(latencies), 4) or round(time.time() - started, 4),
        model=f"(strategy: {selection.winner.provider} of {len(selection.proposals)})",
    )


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
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "implement",
            "summary": _summarize(prose or resp.text),
            "model": resp.model,
            "wrote_solution_py": bool(code),
        }
    )
    return resp


def run_verify(task_dir: TaskDir, config: Config) -> ModelResponse:
    """Score the latest implementation.

    v0.2 multi-axis rubric path: if ``artifacts/rubric.json`` exists, the
    Verify Engine drives programmatic evaluators (pytest / benchmark /
    ast_grep / llm_rubric) and writes a richer ``solution.json``.
    Otherwise we fall back to the v0.1 single-call LLM verifier
    (``_run_verify_llm_legacy``).
    """
    rubric_path = task_dir.artifact_path("rubric.json")
    if rubric_path.exists():
        return _run_verify_with_rubric(task_dir, config, rubric_path)
    return _run_verify_llm_legacy(task_dir, config)


def _run_verify_with_rubric(task_dir: TaskDir, config: Config, rubric_path: Path) -> ModelResponse:
    """Programmatic + (optional) LLM-rubric evaluation driven by rubric.json."""
    # Late import to avoid a cycle (verify_engine imports state + config).
    from agent_loop.verify_engine import VerifyEngine, result_to_dict

    started = time.time()
    try:
        rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # malformed rubric -> fall back to legacy
        return _run_verify_llm_legacy(task_dir, config)

    engine = VerifyEngine(task_dir, config)
    result = engine.evaluate(rubric)
    payload = result_to_dict(result)
    payload["evidence"] = result.summary  # backward-compat surface
    task_dir.write_artifact("solution.json", payload)

    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "verify",
            "summary": _summarize(result.summary),
            "score": float(result.weighted_score),
            "model": "(verify_engine: rubric)",
        }
    )

    return ModelResponse(
        text=json.dumps(payload),
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
        latency_s=round(time.time() - started, 4),
        model="(verify_engine: rubric)",
    )


def _run_verify_llm_legacy(task_dir: TaskDir, config: Config) -> ModelResponse:
    """v0.1 single-call LLM verifier. Preserved verbatim for backward compat."""
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
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "verify",
            "summary": _summarize(str(parsed.get("evidence", "")) or resp.text),
            "score": float(parsed.get("weighted_score", 0.0) or 0.0),
            "model": resp.model,
        }
    )
    return resp


def run_judge(task_dir: TaskDir, config: Config) -> ModelResponse:
    """Compare current solution.json to best_solution.json and decide next action.

    Two modes:
      - **single** (default): one LLM judge as configured by ``config.models.judge``.
        Includes the first-cycle short-circuit (no LLM call when there is no
        ``best_solution.json`` yet).
      - **multi-judge** (v0.3): when ``config.runtime.judges`` is non-empty,
        ``_run_judge_multi`` fans out to N providers in parallel and writes a
        ``consensus`` payload alongside the canonical schema. The first-cycle
        short-circuit still applies (no fan-out cost on cycle 1).
    """
    if config.runtime.judges:
        return _run_judge_multi(task_dir, config)
    return _run_judge_single(task_dir, config)


def _run_judge_single(task_dir: TaskDir, config: Config) -> ModelResponse:
    """v0.1/v0.2 body: one LLM judge call. Preserved verbatim for backward compat.

    The first-cycle short-circuit (no LLM call when there is no prior best)
    can be disabled by setting ``runtime.judge_always_llm = True``. In that
    mode, the LLM is invoked even on cycle 1 with an empty ``best_solution``
    payload, which is required for genuine multi-judge cross-vendor
    verification when the verifier returns score>=0.95 on cycle 1.
    """
    if not task_dir.has_artifact("best_solution.json") and not config.runtime.judge_always_llm:
        # First cycle: nothing to compare against (and short-circuit not disabled).
        sol = task_dir.read_artifact("solution.json") if task_dir.has_artifact("solution.json") else {}
        # weighted_score is the v0.2 canonical field; v0.1 wrote `score` only.
        score = (
            sol.get("weighted_score", sol.get("score", 0.0))
            if isinstance(sol, dict)
            else 0.0
        )
        result = {
            "better": True,
            "action": "stop" if score >= 0.95 else "redo_R",
            "reason": "no prior best — first cycle is automatically the best",
            "hint": "next cycle should iterate on weak axes" if score < 0.95 else "",
            "scores": {"this_cycle": score, "best": None, "delta": None},
        }
        task_dir.write_artifact("judge_result.json", result)
        ContextEngine(task_dir).append_history(
            {
                "cycle": _current_cycle(task_dir),
                "phase": "judge",
                "summary": result["reason"],
                "hint": result.get("hint", ""),
                "score": float(score or 0.0),
                "model": "(skipped: first cycle)",
            }
        )
        return ModelResponse(
            text=json.dumps(result),
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_s=0.0,
            model="(skipped: first cycle)",
        )

    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir)
    sol_obj = task_dir.read_artifact("solution.json") if task_dir.has_artifact("solution.json") else {}
    # judge_always_llm + first cycle: no prior best yet. Pass an empty stub so
    # the prompt is still well-formed and the LLM is genuinely invoked.
    best_obj = (
        task_dir.read_artifact("best_solution.json")
        if task_dir.has_artifact("best_solution.json")
        else {}
    )
    sol_str = json.dumps(sol_obj, indent=2, ensure_ascii=False)
    best_str = json.dumps(best_obj, indent=2, ensure_ascii=False)

    max_redo = config.runtime.max_redo
    redo_count = _current_redo_count(task_dir)

    prompt = _load_prompt("judge").format(
        task=task,
        solution=sol_str,
        best_solution=best_str,
        memory=memory,
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
    scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "judge",
            "summary": _summarize(str(parsed.get("reason", "")) or resp.text),
            "hint": parsed.get("hint", ""),
            "score": float(scores.get("this_cycle", 0.0) or 0.0) if isinstance(scores, dict) else 0.0,
            "model": resp.model,
        }
    )
    return resp


def _run_judge_multi(task_dir: TaskDir, config: Config) -> ModelResponse:
    """v0.3 multi-judge: N providers fan out, weighted-majority aggregation.

    First-cycle short-circuit defers to ``_run_judge_single`` (no fan-out cost
    when there is no prior best to compare against). All-judges-fail also
    falls back to single, with the resulting ``judge_result.json`` annotated
    with ``consensus.fallback = True``.

    When ``runtime.judge_always_llm`` is True, the first-cycle short-circuit
    is disabled and the multi-judge fan-out runs even on cycle 1 with an
    empty ``best_solution`` stub — required for genuine cross-vendor
    multi-judge verification on cycle 1.
    """
    if not task_dir.has_artifact("best_solution.json") and not config.runtime.judge_always_llm:
        return _run_judge_single(task_dir, config)

    # Late import: keeps workers.py importable even if judge_engine has issues.
    from agent_loop.judge_engine import (
        AllJudgesFailed,
        JudgeEngine,
        consensus_to_dict,
    )

    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir)
    sol_obj = task_dir.read_artifact("solution.json") if task_dir.has_artifact("solution.json") else {}
    # First cycle + judge_always_llm: best_solution.json may not exist yet.
    best_obj = (
        task_dir.read_artifact("best_solution.json")
        if task_dir.has_artifact("best_solution.json")
        else {}
    )
    sol_str = json.dumps(sol_obj, indent=2, ensure_ascii=False)
    best_str = json.dumps(best_obj, indent=2, ensure_ascii=False)

    max_redo = config.runtime.max_redo
    redo_count = _current_redo_count(task_dir)

    prompt = _load_prompt("judge").format(
        task=task,
        solution=sol_str,
        best_solution=best_str,
        memory=memory,
        redo_count=redo_count,
        max_redo=max_redo,
    )

    engine = JudgeEngine(task_dir, config)
    started = time.time()
    try:
        result = engine.consensus(config.runtime.judges, prompt)
    except AllJudgesFailed as e:
        # Fallback path: every judge errored. Run the single-judge body and
        # annotate the result with consensus.fallback so observers know.
        resp = _run_judge_single(task_dir, config)
        existing = task_dir.read_artifact("judge_result.json")
        if isinstance(existing, dict):
            existing["consensus"] = {
                "n_judges": len(e.individuals),
                "votes_action": {},
                "votes_better": {},
                "fallback": True,
                "individual": [
                    {
                        "provider": i.provider,
                        "weight": i.weight,
                        "better": i.better,
                        "action": i.action,
                        "weighted_score": i.weighted_score,
                        "hint": i.hint,
                        "reason": i.reason,
                        "error": i.error,
                        "latency_s": i.latency_s,
                    }
                    for i in e.individuals
                ],
            }
            task_dir.write_artifact("judge_result.json", existing)
        return resp

    # Compose the canonical judge_result.json schema with consensus payload.
    this_score = result.scores.get("weighted")
    sol_score = (
        sol_obj.get("weighted_score", sol_obj.get("score"))
        if isinstance(sol_obj, dict)
        else None
    )
    best_score = (
        best_obj.get("weighted_score", best_obj.get("score"))
        if isinstance(best_obj, dict)
        else None
    )
    if this_score is None:
        this_score = sol_score
    delta: float | None = None
    if isinstance(this_score, (int, float)) and isinstance(best_score, (int, float)):
        delta = float(this_score) - float(best_score)

    payload: dict[str, Any] = {
        "better": result.better,
        "action": result.action,
        "reason": result.reason,
        "hint": result.hint,
        "scores": {
            "weighted": this_score,
            "this_cycle": this_score,
            "best": best_score,
            "delta": delta,
        },
        "consensus": consensus_to_dict(result),
    }
    task_dir.write_artifact("judge_result.json", payload)

    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "judge",
            "summary": _summarize(result.reason),
            "hint": result.hint,
            "score": float(this_score or 0.0),
            "model": f"(consensus: {result.n_judges} judges)",
            "n_judges": result.n_judges,
        }
    )

    # Aggregate ModelResponse: cost = sum, latency = max (parallel critical path)
    costs = sum(0.0 for _ in result.individual)  # CLI providers report 0.0; safe default
    latencies = [j.latency_s for j in result.individual if j.error is None] or [0.0]
    return ModelResponse(
        text=json.dumps(payload),
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=costs,
        latency_s=round(max(latencies), 4) or round(time.time() - started, 4),
        model=f"(consensus: {result.n_judges} judges)",
    )


def _current_redo_count(task_dir: TaskDir) -> int:
    """Pull redo_count from the latest checkpoint (default 0)."""
    cp = task_dir.load_latest_checkpoint()
    if not cp:
        return 0
    return int(cp.get("payload", {}).get("redo_count", 0))
