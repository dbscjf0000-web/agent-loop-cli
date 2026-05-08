"""LLM-as-judge rubric evaluator (soft axis fallback).

Used for axes that cannot be measured programmatically (idiomaticity,
documentation quality, ...). The verdict is *not* ground truth — the
VerifyEngine flags it explicitly so the orchestrator/judge can prefer
hard signals.

Spec keys:
    weight    (float)
    criterion (str) plain-English description of what to score.
    timeout   (float, optional, default 30) verify-phase model timeout

The evaluator goes through ``models.call_model("verify", ...)`` so it
inherits the configured verify-phase model. A small prompt asks for a
strict JSON object ``{"score": 0..1, "evidence": "..."}``.
"""
from __future__ import annotations

import json
import re
from typing import Any

from agent_loop.config import Config
from agent_loop.state import TaskDir
from agent_loop.verify_types import AxisScore

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty response")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(s[a : b + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"could not parse JSON (len={len(s)})")


def _build_prompt(name: str, criterion: str, source: str, source_kind: str = "code") -> str:
    return (
        f"Score the following {source_kind} on the rubric axis '{name}': {criterion}\n\n"
        "Output a single JSON object with exactly two keys:\n"
        '  "score":    a float in [0, 1]\n'
        '  "evidence": a one-sentence justification\n\n'
        "Respond with JSON only.\n\n"
        f"===== {source_kind} =====\n"
        f"{source[:6000]}\n"
        "===== end =====\n"
    )


def run_llm_rubric(
    *,
    name: str,
    spec: dict[str, Any],
    task_dir: TaskDir,
    config: Config,
) -> AxisScore:
    # Late import — keeps `models.py` cycle-free and lets tests stub
    # `agent_loop.evaluators.llm_rubric.call_model` without importing litellm.
    from agent_loop.models import call_model

    weight = float(spec.get("weight", 1.0) or 0.0)
    criterion = str(spec.get("criterion") or spec.get("description") or "code quality")

    # v0.12.0 follow-up — non-code rubric axes (manuscript, spec, …) need to
    # see the actual artifact, not a missing solution.py. Same `spec["file"]`
    # contract as pytest_runner; default keeps backward-compat.
    src_file = str(spec.get("file") or "solution.py")
    from agent_loop.workers import _is_safe_workspace_filename
    if not _is_safe_workspace_filename(src_file):
        return AxisScore(
            name=name, score=0.0, weight=weight, evaluator="llm_rubric",
            evidence=f"unsafe spec.file: {src_file!r}", is_ground_truth=False,
        )
    sol = task_dir.workspace_path() / src_file
    source = sol.read_text(encoding="utf-8") if sol.exists() else f"(no {src_file})"
    # Adapt prompt language to the artifact kind so the model doesn't apply a
    # code-style rubric to a markdown/json/text file.
    ext = src_file.rsplit(".", 1)[-1].lower() if "." in src_file else ""
    source_kind = {
        "py": "code", "js": "code", "ts": "code", "rb": "code", "go": "code",
        "md": "document", "txt": "document", "rst": "document",
        "json": "data", "yaml": "data", "yml": "data", "toml": "data",
        "html": "document", "tex": "document",
    }.get(ext, "artifact")

    prompt = _build_prompt(name, criterion, source, source_kind)
    try:
        resp = call_model(
            "verify",
            prompt,
            system="You are a strict code reviewer. Output JSON only.",
            config=config,
            workspace=task_dir.workspace_path(),
        )
    except Exception as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="llm_rubric",
            evidence=f"call_model failed: {type(exc).__name__}: {exc}",
            is_ground_truth=False,
        )

    try:
        parsed = _extract_json(resp.text)
    except ValueError as exc:
        return AxisScore(
            name=name,
            score=0.0,
            weight=weight,
            evaluator="llm_rubric",
            evidence=f"unparseable rubric response: {exc}",
            is_ground_truth=False,
            raw={"raw": (resp.text or "")[:1000]},
        )

    score = float(parsed.get("score", 0.0) or 0.0)
    evidence = str(parsed.get("evidence", ""))[:400]
    return AxisScore(
        name=name,
        score=max(0.0, min(1.0, score)),
        weight=weight,
        evaluator="llm_rubric",
        evidence=evidence or "(no evidence given)",
        is_ground_truth=False,
        raw={"model": resp.model, "tokens_in": resp.prompt_tokens, "tokens_out": resp.completion_tokens},
    )


__all__ = ["run_llm_rubric"]
