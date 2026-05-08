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


import logging as _logging
log = _logging.getLogger(__name__)


def _count_plan_subtasks(plan_text: str) -> int:
    """Count occurrences of `### subtask-` headers in plan.md (case-insensitive).

    Returns 0 if plan does not use the structured sub-task format — then
    the soft-check is silently skipped.
    """
    import re as _re
    if not plan_text:
        return 0
    return len(_re.findall(r"^###\s+subtask-", plan_text, flags=_re.IGNORECASE | _re.MULTILINE))


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


def _extract_test_subtask_files(text: str) -> dict[str, str]:
    """Backward-compat wrapper kept for existing call sites and tests.

    Returns only ``test_subtask*.py`` files from the generalized
    ``_extract_workspace_files`` output.
    """
    import re as _re
    all_files = _extract_workspace_files(text)
    out: dict[str, str] = {}
    for fname, body in all_files.items():
        if _re.match(r"^test_subtask[A-Za-z0-9_]*\.py$", fname):
            out[fname] = body
    return out


import re as _vre  # v0.12.0 — module-level so we don't __import__ on every call.

# Path traversal defense: filenames must match this strict pattern so that an
# LLM cannot write outside workspace via headers like ``# file: ../etc/passwd``.
_SAFE_FILENAME_RE = _vre.compile(r"^[A-Za-z0-9_\-.]+$")
# Match any fenced code block: ```<lang> ... ``` (lang is optional).
_GENERIC_FENCE_RE = _vre.compile(
    r"```([A-Za-z0-9_\-+]*)\s*\n(.*?)```",
    _vre.DOTALL,
)
# Match a `# file: <name>` header. Supports several comment styles.
_FILE_HEADER_RE = _vre.compile(
    r"^\s*(?:#|//|--|;|<!--|/\*)\s*file\s*:\s*([A-Za-z0-9_\-.]+)\s*(?:-->|\*/)?\s*$"
)


def _is_safe_workspace_filename(name: str) -> bool:
    """Reject path traversal and absolute paths. Allowed: ``A-Z a-z 0-9 _ - .``."""
    if not name or len(name) > 255:
        return False
    if name in (".", "..") or name.startswith("."):  # also block dotfiles like .bashrc
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_SAFE_FILENAME_RE.match(name))


def _extract_workspace_files(text: str) -> dict[str, str]:
    """v0.12.0 — extract all ``# file: <name>`` annotated fenced blocks from
    an Implement worker response into a ``{filename: body}`` mapping.

    Rules:
      - Block format: ```<lang>\\n# file: <name>\\n<body>\\n```
      - Header on the first non-blank line of the block; comment style varies
        by language (#, //, --, ;, <!-- -->, /* */).
      - Filenames are strictly validated (path traversal blocked).
      - Header line itself is stripped from the saved body.
      - Blocks without a recognized header are ignored here (the legacy
        ``solution.py`` default for the first python block is handled by
        ``run_implement`` for backward-compat).
      - Last duplicate filename wins (LLM may emit revised version).

    Pure parsing — no I/O, no path resolution. Caller is responsible for
    saving the body under the workspace dir.
    """
    out: dict[str, str] = {}
    for _lang, raw in _GENERIC_FENCE_RE.findall(text):
        # Find the first non-blank line — Codex review fix: previous logic
        # only stripped leading newlines, so a block starting with a
        # whitespace-only line silently lost its header.
        lines = raw.splitlines()
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            continue
        first_line = lines[idx]
        m = _FILE_HEADER_RE.match(first_line)
        if not m:
            continue
        fname = m.group(1).strip()
        if not _is_safe_workspace_filename(fname):
            log.warning(
                "implement: rejected unsafe filename in # file: header: %r", fname
            )
            continue
        rest = "\n".join(lines[idx + 1 :])
        out[fname] = rest.rstrip() + "\n"
    return out


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


def _memory_text(task_dir: TaskDir, config: Config) -> str:
    """Render the v0.2 ContextEngine snapshot as the ``{memory}`` prompt slot.

    Falls back to the legacy ``memory.txt`` content when the engine has no data
    yet (very first phase of a fresh task) so the prompt never sees a blank
    string when something useful is available.

    v0.4.2: ``config`` is now required so cross-task settings (``--no-cross-task``
    / ``runtime.cross_task_memory*``) are honored inside phase workers. Earlier
    versions silently constructed a default ``ContextEngine(task_dir)`` here,
    which made workers ignore the user's privacy flag.
    """
    eng = ContextEngine(
        task_dir,
        global_root=config.runtime.cross_task_memory_dir,
        cross_task=config.runtime.cross_task_memory,
        global_max_chars=config.runtime.cross_task_memory_max_chars,
    )
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


def _collect_prior_cycles_summary(task_dir: TaskDir, *, max_chars: int = 2000) -> str:
    """Render a per-cycle summary of past attempts for the Judge prompt.

    v0.6 Judge enhancement. The single-LLM and multi-judge paths both inject
    this string into the ``{prior_cycles}`` placeholder so the judge can:
      - Detect axes that have been < 0.5 for two+ cycles (stuck-axis pivot).
      - See prior hints verbatim and avoid repeating them.
      - Glance at the last attempted ``solution.py`` to ground its reasoning.

    Returns ``""`` (empty string) when there are no prior cycles yet (cycle 1)
    so the prompt template still renders cleanly without spurious "no prior"
    boilerplate clutter.

    Data sources (all already on disk; no extra LLM cost):
      1. ``memory/history.jsonl`` — per-phase audit rows. We collect verify
         rows (score / axes summary) and judge rows (prior hint + reason).
      2. ``workspace/best_solution.py`` — the last accepted solution code.
         Truncated to keep the prompt small.

    Output format (markdown bullet list, fits the prompt nicely)::

        - Cycle 1: weighted_score=0.70, axes={correctness:1.0, perf:0.0, ...},
          hint_received="(none)", attempted_excerpt="def find(s): ..."
        - Cycle 2: weighted_score=0.70, axes={..., perf:0.0},
          hint_received="perf axis again", attempted_excerpt="(unchanged)"

    The ``max_chars`` cap is enforced *after* assembly. We truncate from the
    front (older cycles fall off first) so the most recent attempt always
    survives — that's what the judge most needs to reason about a pivot.
    """
    history_path = task_dir.memory_dir() / "history.jsonl"
    if not history_path.exists():
        return ""

    # Group rows by cycle — only verify/judge rows carry useful score+hint data.
    by_cycle: dict[int, dict[str, Any]] = {}
    try:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cyc = rec.get("cycle")
                if not isinstance(cyc, int):
                    continue
                phase = rec.get("phase")
                if phase not in ("verify", "judge"):
                    continue
                slot = by_cycle.setdefault(cyc, {})
                if phase == "verify":
                    slot["score"] = rec.get("score")
                    # ContextEngine writes a summary string; we keep it raw so
                    # the judge can read the axis labels embedded in it.
                    slot["verify_summary"] = rec.get("summary") or ""
                elif phase == "judge":
                    # Judge hint is what the *previous* judge told the loop to
                    # do — exactly what we want to flag as "do not repeat".
                    slot["judge_hint"] = rec.get("hint") or ""
                    slot["judge_reason"] = rec.get("summary") or ""
    except OSError:
        return ""

    if not by_cycle:
        return ""

    # Pull axes details from the canonical solution.json (current cycle's
    # latest verify result — gives us per-axis score, not just summary).
    # We add this only for the most recent cycle since older axes are not
    # easily reconstructible from history alone.
    latest_axes_text = ""
    if task_dir.has_artifact("solution.json"):
        sol = task_dir.read_artifact("solution.json")
        if isinstance(sol, dict):
            axes = sol.get("axes")
            if isinstance(axes, list):
                pairs: list[str] = []
                for ax in axes:
                    if not isinstance(ax, dict):
                        continue
                    name = ax.get("name") or "?"
                    score = ax.get("score")
                    if isinstance(score, (int, float)):
                        pairs.append(f"{name}:{float(score):.2f}")
                if pairs:
                    latest_axes_text = "{" + ", ".join(pairs) + "}"

    # Last attempted code excerpt (short).
    sol_py = task_dir.workspace_path() / "solution.py"
    code_excerpt = ""
    if sol_py.exists():
        try:
            raw = sol_py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        code_excerpt = raw[:500].rstrip()

    # Render bullets in cycle order.
    lines: list[str] = ["## Prior cycles"]
    for cyc in sorted(by_cycle.keys()):
        slot = by_cycle[cyc]
        score = slot.get("score")
        score_txt = f"{float(score):.2f}" if isinstance(score, (int, float)) else "?"
        hint = (slot.get("judge_hint") or "(none)").strip().replace("\n", " ")
        if len(hint) > 200:
            hint = hint[:197] + "..."
        verify_sum = (slot.get("verify_summary") or "").strip().replace("\n", " ")
        if len(verify_sum) > 200:
            verify_sum = verify_sum[:197] + "..."
        # First line of the bullet — score + hint received from this cycle's judge.
        bullet = (
            f"- Cycle {cyc}: weighted_score={score_txt}, "
            f"hint_received=\"{hint}\""
        )
        if verify_sum:
            bullet += f", verify=\"{verify_sum}\""
        lines.append(bullet)

    if latest_axes_text:
        lines.append(f"- Latest axes: {latest_axes_text}")
    if code_excerpt:
        lines.append("- Last attempted code (excerpt):")
        lines.append("```python")
        lines.append(code_excerpt)
        lines.append("```")

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered

    # Truncate from the front (drop older cycles first).
    # We rebuild instead of slicing string mid-line.
    keep_lines = lines[:1]  # always keep "## Prior cycles" header
    cycle_lines = lines[1:-2 if code_excerpt else len(lines)]  # may include axes
    tail_lines = lines[-2:] if code_excerpt else []
    while cycle_lines and len("\n".join(keep_lines + cycle_lines + tail_lines)) > max_chars:
        cycle_lines = cycle_lines[1:]
    final = "\n".join(keep_lines + cycle_lines + tail_lines)
    if len(final) > max_chars:
        # Last resort: hard truncate.
        final = final[: max_chars - 4] + "\n..."
    return final


def _collect_prior_judge_hint(task_dir: TaskDir, *, max_chars: int = 1000) -> str:
    """Return the most recent judge ``hint`` (verbatim) from prior cycles, or ``""``.

    v0.7 Plan enhancement. The Plan worker injects this string into the
    ``{prior_judge_hint}`` placeholder so the planner is forced to honor the
    judge's structural recommendation (e.g. "use Manacher's algorithm O(n)")
    instead of regenerating the same approach that already produced the
    current (insufficient) weighted_score.

    Empty string is returned when:
      - history.jsonl does not exist (cycle 1, fresh task), or
      - no judge row in history.jsonl carries a non-empty ``hint`` field, or
      - the file cannot be opened.

    The string is capped at ``max_chars`` (default 1000) so a runaway hint
    cannot inflate the plan prompt. Truncation is suffixed with "...".

    Data sources (in priority order, both already on disk — no LLM cost):
      1. ``memory/history.jsonl`` — search backwards for the last
         ``phase=judge`` row whose ``hint`` is a non-empty string. This is
         the canonical place workers append per-phase audit data.
      2. ``artifacts/judge_result.json`` — fallback for the case where the
         orchestrator wrote the file but the history row was lost (rare, but
         this artifact persists across resume so we honor it).

    Notes:
      - We do *not* filter by current cycle. The "most recent prior hint" is
        whatever the most recent judge call produced. On cycle 1 there are
        no judge rows yet → empty string returned, which is the right answer.
      - We deliberately return the hint verbatim (no rewording, no extra
        framing) so the Plan prompt section can show it inside fenced text
        without surprises.
    """
    history_path = task_dir.memory_dir() / "history.jsonl"
    hint = ""
    if history_path.exists():
        try:
            with history_path.open("r", encoding="utf-8") as f:
                rows = f.readlines()
        except OSError:
            rows = []
        # Walk backward — newest entries are at the bottom of the JSONL file.
        for line in reversed(rows):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("phase") != "judge":
                continue
            h = rec.get("hint")
            if isinstance(h, str) and h.strip():
                hint = h.strip()
                break

    # Fallback: judge_result.json (last completed judge of any prior cycle).
    if not hint and task_dir.has_artifact("judge_result.json"):
        try:
            jr = task_dir.read_artifact("judge_result.json")
        except Exception:  # pragma: no cover - defensive
            jr = None
        if isinstance(jr, dict):
            h = jr.get("hint")
            if isinstance(h, str) and h.strip():
                hint = h.strip()

    if not hint:
        return ""

    # Length cap (defensive: hints are usually short, but a model could ramble).
    if len(hint) > max_chars:
        hint = hint[: max_chars - 3].rstrip() + "..."
    return hint


def _build_prior_context_block(task_dir: TaskDir) -> str:
    """Render the Plan prompt's prior-context block, or "" on cycle 1.

    v0.7.1 (codex review fix): cycle 1 has no prior data, so injecting
    sentinel sections + Reasoning Constraints just makes the model spend
    extra reasoning verifying that sentinels really mean "nothing to do".
    Returning an empty string here means cycle 1 prompts are byte-identical
    to v0.6 — no constraint-evaluation overhead — while cycle 2+ get the
    full block (hint + prior cycles + rules) only when there is real
    context to honor.
    """
    hint = _collect_prior_judge_hint(task_dir)
    cycles = _collect_prior_cycles_summary(task_dir)
    if not hint and not cycles:
        return ""

    parts: list[str] = ["", ""]  # blank line separator from {findings}
    if hint:
        parts += ["### Prior Judge Hint", "```", hint, "```", ""]
    if cycles:
        parts += [
            "### Prior Cycles (recent attempts + algorithms)",
            "```",
            cycles,
            "```",
            "",
        ]
    parts += [
        "## Reasoning Constraints",
        "- Your plan MUST honor the Prior Judge Hint above (if present): use the",
        "  named algorithm / library / measurement approach exactly as recommended,",
        "  and cite it explicitly, e.g. \"Following Judge's recommendation to use",
        "  Manacher's algorithm, ...\".",
        "- Do NOT propose the same algorithm family any prior cycle's",
        "  attempted_excerpt already used. Name the rejected family in section 2's",
        "  \"대안과 기각 이유\".",
        "- If the hint specifies a library or measurement tool (e.g. timeit,",
        "  pytest-benchmark), include it in section 4 (Verification Plan).",
    ]
    return "\n".join(parts)


def run_research(task_dir: TaskDir, config: Config) -> ModelResponse:
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir, config)
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
    # v0.4.1 auto-rubric: generate a multi-axis rubric so free-form tasks get
    # the same multi-axis verify experience as bench (yaml-driven). Skipped
    # when (a) auto_rubric is disabled, (b) a hand-written rubric already
    # exists, or (c) a previous cycle already wrote rubric_auto.json.
    # v0.4.2: any auto-rubric cost / latency is folded into the returned
    # research ModelResponse so the orchestrator's total_cost (and budget
    # guard) includes it. A dedicated `_auto_rubric` row in metrics.jsonl
    # preserves audit granularity.
    extra = _maybe_generate_auto_rubric(task_dir, config, task, resp.text)
    if extra is not None:
        resp = ModelResponse(
            text=resp.text,
            prompt_tokens=resp.prompt_tokens + extra.prompt_tokens,
            completion_tokens=resp.completion_tokens + extra.completion_tokens,
            cost_usd=round(resp.cost_usd + extra.cost_usd, 6),
            latency_s=round(resp.latency_s + extra.latency_s, 4),
            model=resp.model,
        )
    return resp


def _maybe_generate_auto_rubric(
    task_dir: TaskDir,
    config: Config,
    task_text: str,
    findings_text: str,
) -> ModelResponse | None:
    """Best-effort auto-rubric generation. Errors are isolated — never raise.

    Writes ``artifacts/rubric_auto.json`` on success. Logs a one-line
    summary to ``memory/history.jsonl`` so the audit trail captures the
    extra LLM call. v0.4.2: also appends a ``phase=_auto_rubric`` row to
    ``telemetry/metrics.jsonl`` so the underlying LLM call's cost / tokens
    / latency are tracked (previously dropped on the floor). The leading
    underscore mirrors ``_cycle_quality`` and avoids the orchestrator's
    hardcoded phase set.

    Returns the underlying ``ModelResponse`` so ``run_research`` can fold the
    cost / latency back into the response it returns to the orchestrator
    (otherwise auto-rubric cost would slip past the per-run budget guard).
    Returns ``None`` when generation was skipped or failed.

    Skips when:
      - ``runtime.auto_rubric`` is False,
      - ``artifacts/rubric.json`` already exists (hand-written / yaml),
      - ``artifacts/rubric_auto.json`` already exists (resume / re-research).
    """
    if not getattr(config.runtime, "auto_rubric", True):
        return None
    if task_dir.has_artifact("rubric.json"):
        return None
    if task_dir.has_artifact("rubric_auto.json"):
        return None

    try:
        from agent_loop.auto_rubric import generate_rubric

        gen = generate_rubric(task_text, findings_text, config)
    except Exception as e:  # pragma: no cover - defensive
        ContextEngine(task_dir).append_history(
            {
                "cycle": _current_cycle(task_dir),
                "phase": "research",
                "summary": f"auto_rubric: skipped ({type(e).__name__}: {e})",
                "model": "(auto_rubric: failed)",
                "auto_rubric": False,
            }
        )
        return None

    rubric = gen.rubric
    resp = gen.response
    task_dir.write_artifact("rubric_auto.json", rubric)
    axes = list((rubric.get("axes") or {}).keys())
    cycle = _current_cycle(task_dir)
    # v0.4.2: dedicated metric row so cost/latency/tokens are not lost. The
    # `_auto_rubric` phase prefix sidesteps the orchestrator's hardcoded
    # phase set (matches the existing `_cycle_quality` convention).
    task_dir.append_metric(
        {
            "cycle": cycle,
            "phase": "_auto_rubric",
            "model": resp.model,
            "tokens_in": resp.prompt_tokens,
            "tokens_out": resp.completion_tokens,
            "cost_usd": resp.cost_usd,
            "latency_s": resp.latency_s,
            "n_axes": len(axes),
        }
    )
    ContextEngine(task_dir).append_history(
        {
            "cycle": cycle,
            "phase": "research",
            "summary": f"auto_rubric: {len(axes)} axes -> {','.join(axes)}",
            "model": "(auto_rubric)",
            "auto_rubric": True,
            "n_axes": len(axes),
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
    """v0.1 / v0.2 body: single LLM plan call.

    v0.7: now also passes ``prior_judge_hint`` so the planner is forced to
    honor the most recent judge recommendation (e.g. "use Manacher's
    algorithm O(n)"). On cycle 1 the helper returns "" and the prompt's
    "Prior Judge Hint" block is rendered empty — equivalent to the prior
    behavior.
    """
    task = task_dir.task_md_path().read_text(encoding="utf-8")
    memory = _memory_text(task_dir, config)
    findings = _read_or(task_dir, "findings.md", "(no findings)")
    prompt = _load_prompt("plan").format(
        task=task,
        memory=memory,
        findings=findings,
        prior_context_block=_build_prior_context_block(task_dir),
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
    memory = _memory_text(task_dir, config)
    findings = _read_or(task_dir, "findings.md", "(no findings)")
    # v0.7.1: same prior-context block as single mode so every multi-strategy
    # candidate honors judge hint + sees prior algorithms. Block is empty on
    # cycle 1 → no Reasoning Constraints overhead when there is nothing to
    # constrain (codex review fix: avoids cursor-agent deliberating on
    # always-true sentinel conditions).
    prompt = _load_prompt("plan").format(
        task=task,
        memory=memory,
        findings=findings,
        prior_context_block=_build_prior_context_block(task_dir),
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
    # Codex review fix #5: clear stale test_subtask*.py from previous cycle
    # so a deleted/renamed sub-task does not silently linger and get
    # promoted to the regression bank.
    for stale in ws.glob("test_subtask*.py"):
        try:
            stale.unlink()
        except OSError:
            pass
    resp = call_model(
        "implement",
        prompt,
        system=_SYSTEM_PROMPT,
        config=config,
        workspace=ws,
    )

    # v0.12.0 — generalized output contract:
    #   1. Any fenced block with `# file: <name>` header → saved verbatim.
    #   2. If no headered solution.py block exists, the first ```python``` block
    #      without a header is treated as the legacy solution.py (backward-compat
    #      for older code tasks whose plans don't mention file names).
    workspace_files = _extract_workspace_files(resp.text)
    legacy_code = ""
    legacy_prose = ""
    if "solution.py" not in workspace_files:
        legacy_code, legacy_prose = _extract_python(resp.text)
        if legacy_code:
            workspace_files["solution.py"] = legacy_code

    for fname, body in workspace_files.items():
        (ws / fname).write_text(body, encoding="utf-8")

    # Soft-check warnings (never fail the run).
    sol_code = workspace_files.get("solution.py", "")
    if sol_code and ("def test_" in sol_code and "import pytest" in sol_code):
        log.warning(
            "implement: solution.py looks test-like — did you swap solution and test?"
        )
    plan_subtasks = _count_plan_subtasks(plan)
    test_files_count = sum(
        1 for n in workspace_files if n.startswith("test_subtask")
    )
    if plan_subtasks > 0 and test_files_count < plan_subtasks:
        log.warning(
            "implement: plan.md declares %d sub-tasks but only %d "
            "test_subtask*.py files were extracted",
            plan_subtasks, test_files_count,
        )

    # Prose = anything outside fenced blocks. We compute it after extraction
    # by re-using legacy_prose when we fell back, otherwise stripping all
    # fenced blocks from resp.text.
    if legacy_prose:
        prose = legacy_prose
    else:
        prose = _GENERIC_FENCE_RE.sub("", resp.text).strip()
    task_dir.write_artifact("execution_log.md", prose or resp.text)
    ContextEngine(task_dir).append_history(
        {
            "cycle": _current_cycle(task_dir),
            "phase": "implement",
            "summary": _summarize(prose or resp.text),
            "model": resp.model,
            "wrote_solution_py": "solution.py" in workspace_files,
        }
    )
    return resp


def run_verify(task_dir: TaskDir, config: Config) -> ModelResponse:
    """Score the latest implementation.

    Priority (v0.4.1):
      1. ``artifacts/rubric.json``       — hand-written / yaml-derived (bench).
      2. ``artifacts/rubric_auto.json``  — Research-phase auto-generated (free-form).
                                           Only used when ``runtime.auto_rubric`` True.
      3. ``_run_verify_llm_legacy``      — single-shot LLM verifier (v0.1 compat).

    Both rubric paths flow through ``VerifyEngine.evaluate`` and write the
    same v0.2 ``solution.json`` schema.
    """
    rubric_path = task_dir.artifact_path("rubric.json")
    if rubric_path.exists():
        return _run_verify_with_rubric(task_dir, config, rubric_path)
    auto_path = task_dir.artifact_path("rubric_auto.json")
    if getattr(config.runtime, "auto_rubric", True) and auto_path.exists():
        return _run_verify_with_rubric(task_dir, config, auto_path)
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

    # Step C — sub-task verifier dispatcher (TDD integration). Adds a
    # ``subtask_verify`` section to solution.json. Failures here do NOT
    # change weighted_score (rubric remains the score authority); they
    # surface as evidence for J to audit.
    try:
        from agent_loop.subtask_verify import (
            run_subtask_verifications, result_to_dict as st_to_dict,
        )
        plan_text = _read_or(task_dir, "plan.md", "")
        st_results = run_subtask_verifications(plan_text, task_dir.workspace_path())
        if st_results:
            payload["subtask_verify"] = [st_to_dict(r) for r in st_results]
    except Exception as e:  # never break verify on TDD bookkeeping
        payload["subtask_verify_error"] = f"{type(e).__name__}: {e}"

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
    memory = _memory_text(task_dir, config)
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
    # v0.6: stuck-axis pivot context. Empty string on cycle 1 (no history).
    prior_cycles = _collect_prior_cycles_summary(task_dir)

    prompt = _load_prompt("judge").format(
        task=task,
        solution=sol_str,
        best_solution=best_str,
        memory=memory,
        redo_count=redo_count,
        max_redo=max_redo,
        prior_cycles=prior_cycles,
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
    memory = _memory_text(task_dir, config)
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
    # v0.6: same stuck-axis context as single-judge so multi-judge can pivot too.
    prior_cycles = _collect_prior_cycles_summary(task_dir)

    prompt = _load_prompt("judge").format(
        task=task,
        solution=sol_str,
        best_solution=best_str,
        memory=memory,
        redo_count=redo_count,
        max_redo=max_redo,
        prior_cycles=prior_cycles,
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
