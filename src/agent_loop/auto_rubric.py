"""Auto-rubric generation (v0.4.1).

For free-form ``agent-loop run "..."`` tasks (no benchmark YAML), the user
gets only a single-shot LLM verify. v0.4.1 closes this gap: after Research
finishes, the orchestrator asks the LLM to *propose* a multi-axis rubric
based on the task description and ``findings.md``. The result lands in
``artifacts/rubric_auto.json`` and is picked up by the Verify Engine
exactly like a hand-written / yaml-derived rubric.

Design contract:
- All axes are ``evaluator: "llm_rubric"`` (no executable axes — that's
  v0.4.2 once we have a code-aware generator).
- Minimum 2 axes (single-axis rubric is degenerate; falls back to the
  legacy LLM verifier instead).
- Weights are normalised so they sum to 1.0; small drifts get fixed
  silently. Zero / negative weights raise.
- Parsing failures raise ``RuntimeError`` so the caller can decide whether
  to fall back to the legacy verifier (``workers.run_research`` does).

Public surface:
    generate_rubric(task_text, findings_text, config) -> RubricGeneration
    RUBRIC_SCHEMA_PROMPT: str

v0.4.2: ``generate_rubric`` now returns a :class:`RubricGeneration` so the
caller can persist the underlying ``ModelResponse`` (cost / tokens / latency)
into ``metrics.jsonl`` instead of dropping it on the floor. The rubric dict
is still the same shape that :py:class:`agent_loop.verify_engine.VerifyEngine.
evaluate` expects, so downstream code is unchanged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agent_loop.config import Config
from agent_loop.models import ModelResponse, call_model

__all__ = ["generate_rubric", "RubricGeneration", "RUBRIC_SCHEMA_PROMPT"]


@dataclass
class RubricGeneration:
    """Bundle the validated rubric with the underlying ``ModelResponse``.

    Returned by :func:`generate_rubric` so callers can record the LLM call's
    cost / tokens / latency into telemetry. The ``rubric`` field has the
    same shape :py:class:`agent_loop.verify_engine.VerifyEngine.evaluate`
    expects.
    """

    rubric: dict[str, Any]
    response: ModelResponse


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
RUBRIC_SCHEMA_PROMPT = """\
You are designing a *multi-axis evaluation rubric* for a programming task.
The rubric will be applied later by an automated verifier; each axis gets
a separate LLM call that returns 0.0..1.0 for that axis.

Input:
- Task description (free-form prose).
- Findings document (facts the researcher already extracted).

Output:
- A single JSON object — no prose, no markdown fences.

Hard schema:
{
  "axes": {
    "<axis_name>": {
      "weight": <float, sum to 1.0>,
      "evaluator": "llm_rubric",
      "criterion": "<one-sentence pass/fail criterion, concrete + checkable>"
    },
    ...
  }
}

Rules:
- Use 2-5 axes. More than 5 means the rubric is too granular for an LLM rubric.
- "correctness" axis is strongly recommended (highest weight, often 0.4-0.6).
- Other typical axes: "edge_cases", "performance", "robustness",
  "code_quality", "readability". Pick what fits THIS task.
- Each criterion must be one self-contained sentence the verifier can check
  by reading the candidate solution. Avoid vague language like "good"/"clean".
- All weights must be > 0. They will be normalised to sum to 1.0; aim for
  approximate 1.0 yourself.
- evaluator MUST be the literal string "llm_rubric" for every axis.
- Do not invent test code, benchmarks, or grep rules — those are out of scope
  for the auto-generator. Stick to llm_rubric.

Output ONLY the JSON object. No preamble, no trailing commentary.
"""


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction (mirrors workers._extract_json)."""
    s = (text or "").strip()
    if not s:
        raise RuntimeError("auto_rubric: empty LLM response")

    try:
        out = json.loads(s)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass

    m = _JSON_FENCE_RE.search(s)
    if m:
        try:
            out = json.loads(m.group(1).strip())
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            out = json.loads(s[start : end + 1])
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        f"auto_rubric: could not parse JSON from LLM response (len={len(s)})"
    )


# ---------------------------------------------------------------------------
# validation + normalisation
# ---------------------------------------------------------------------------
def _validate_and_normalize(rubric: dict[str, Any]) -> None:
    """In-place validate the rubric and normalise axis weights to sum 1.0.

    Raises:
        RuntimeError: malformed schema.
        ValueError: fewer than 2 axes (degenerate).
    """
    if not isinstance(rubric, dict):
        raise RuntimeError("auto_rubric: top-level is not an object")
    axes = rubric.get("axes")
    if not isinstance(axes, dict):
        raise RuntimeError("auto_rubric: missing 'axes' object")
    if len(axes) < 2:
        raise ValueError(
            f"auto_rubric: need >= 2 axes, got {len(axes)} (single-shot rubric is degenerate)"
        )

    cleaned: dict[str, dict[str, Any]] = {}
    total = 0.0
    for name, spec in axes.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"auto_rubric: invalid axis name {name!r}")
        if not isinstance(spec, dict):
            raise RuntimeError(f"auto_rubric: axis {name!r} spec is not an object")

        raw_w = spec.get("weight", 1.0)
        try:
            w = float(raw_w)
        except (TypeError, ValueError):
            raise RuntimeError(f"auto_rubric: axis {name!r} weight not numeric: {raw_w!r}")
        if w <= 0:
            raise RuntimeError(f"auto_rubric: axis {name!r} weight must be > 0 (got {w})")

        criterion = spec.get("criterion") or spec.get("description") or ""
        if not isinstance(criterion, str) or not criterion.strip():
            raise RuntimeError(f"auto_rubric: axis {name!r} missing 'criterion' string")

        cleaned[name.strip()] = {
            "weight": w,
            "evaluator": "llm_rubric",  # force — LLM cannot author code-based axes
            "criterion": criterion.strip(),
        }
        total += w

    if total <= 0:
        raise RuntimeError("auto_rubric: total weight is 0")

    # normalise to sum 1.0 (round to 6 decimals so JSON stays clean)
    for spec in cleaned.values():
        spec["weight"] = round(spec["weight"] / total, 6)

    rubric["axes"] = cleaned


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def generate_rubric(
    task_text: str,
    findings_text: str,
    config: Config,
) -> RubricGeneration:
    """Ask the LLM (research-phase model) to propose a multi-axis rubric.

    Returns a :class:`RubricGeneration` bundling the validated + normalised
    rubric and the underlying ``ModelResponse`` so callers can record the
    LLM call's telemetry (cost / tokens / latency) — v0.4.2 fix for the
    metric-dropping bug.

    Raises ``RuntimeError`` on any LLM / parsing / validation failure so the
    caller can warn-and-fallback gracefully.
    """
    findings_block = (findings_text or "").strip() or "(no findings)"
    prompt = (
        f"{RUBRIC_SCHEMA_PROMPT}\n\n"
        f"### Task\n```\n{task_text.strip()}\n```\n\n"
        f"### Findings\n```\n{findings_block}\n```\n"
    )

    resp = call_model(
        "research",  # reuse research-phase model (no new phase)
        prompt,
        system="You generate JSON rubrics. Output JSON only.",
        config=config,
    )
    rubric = _extract_json(resp.text)
    _validate_and_normalize(rubric)
    return RubricGeneration(rubric=rubric, response=resp)
