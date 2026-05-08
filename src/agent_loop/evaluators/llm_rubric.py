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


def _build_prompt(
    name: str, criterion: str, source: str, source_kind: str = "code",
    *, max_chars: int | None = None,
) -> str:
    """v0.12.0+: callers may pass a custom ``max_chars`` cap. Falls back to
    32 000 chars (~8k tokens) — large enough for manuscript-sized artifacts
    but still bounded so verify cost stays predictable. Set max_chars=None
    or 0 for no cap (rubric author's responsibility)."""
    if max_chars is None:
        body = source
    elif max_chars <= 0:
        body = source
    else:
        body = source[:max_chars]
        if len(source) > max_chars:
            body += f"\n... [truncated {len(source) - max_chars} chars]"
    return (
        f"Score the following {source_kind} on the rubric axis '{name}': {criterion}\n\n"
        "Output a single JSON object with exactly two keys:\n"
        '  "score":    a float in [0, 1]\n'
        '  "evidence": a one-sentence justification\n\n'
        "Respond with JSON only.\n\n"
        f"===== {source_kind} =====\n"
        f"{body}\n"
        "===== end =====\n"
    )


# Default upper bound for an axis's source material. Large enough that a
# manuscript with abstract + intro + methods + discussion + references fits
# but bounded so verify cost stays predictable. Override via
# ``spec["max_bytes"]``.
_DEFAULT_MAX_BYTES = 32_000


def _read_with_head_tail(path: Any, max_bytes: int) -> str:
    """Read a file capped at ``max_bytes``. When the file is more than
    1.5x the cap, return the first half + a divider + the last half so
    references/figure legends at the end of long documents are still
    visible to the rubric.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(read error: {exc})"
    if max_bytes <= 0 or len(text) <= max_bytes:
        return text
    if len(text) >= int(max_bytes * 1.5):
        head = text[: max_bytes // 2]
        tail = text[-(max_bytes // 2):]
        return (
            head
            + f"\n\n... [omitted {len(text) - max_bytes} middle chars; "
              "showing head + tail so references/legends remain visible] ...\n\n"
            + tail
        )
    return text[:max_bytes] + f"\n... [truncated {len(text) - max_bytes} chars]"


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

    # v0.12.0 — accept either a single filename or a list of filenames.
    # Multi-file lets a single rubric axis evaluate against several artifacts
    # together (e.g. ``file: ["task.md", "rubric.json"]`` for a meta-task that
    # builds both). The previous single-string contract silently broke when
    # an LLM emitted ``file: "both"`` or a list — score=0 with no signal.
    raw_file = spec.get("file") or "solution.py"
    src_files: list[str]
    if isinstance(raw_file, list):
        src_files = [str(x) for x in raw_file if x]
        if not src_files:
            src_files = ["solution.py"]
    else:
        src_files = [str(raw_file)]

    from agent_loop.workers import _is_safe_workspace_filename
    for fname in src_files:
        if not _is_safe_workspace_filename(fname):
            return AxisScore(
                name=name, score=0.0, weight=weight, evaluator="llm_rubric",
                evidence=f"unsafe spec.file: {fname!r}", is_ground_truth=False,
            )

    # v0.12.0+ — adaptive head+tail read so manuscript-sized documents have
    # their references / figure legends visible. spec["max_bytes"] overrides
    # the default per-file cap; spec["max_chars"] overrides the prompt cap.
    raw_max_bytes = spec.get("max_bytes")
    try:
        per_file_cap = int(raw_max_bytes) if raw_max_bytes is not None else _DEFAULT_MAX_BYTES
    except (TypeError, ValueError):
        per_file_cap = _DEFAULT_MAX_BYTES

    parts: list[str] = []
    ext_kinds: set[str] = set()
    for fname in src_files:
        p = task_dir.workspace_path() / fname
        if p.exists():
            body = _read_with_head_tail(p, per_file_cap)
        else:
            body = f"(no {fname})"
        if len(src_files) == 1:
            parts.append(body)
        else:
            parts.append(f"===== {fname} =====\n{body}")
        ext_kinds.add((fname.rsplit(".", 1)[-1].lower() if "." in fname else ""))
    source = "\n\n".join(parts)

    # Adapt prompt language to the artifact kind so the model doesn't apply a
    # code-style rubric to a markdown/json/text file. With multiple files we
    # fall back to "artifact" unless every file shares the same kind.
    _kind_map = {
        "py": "code", "js": "code", "ts": "code", "rb": "code", "go": "code",
        "md": "document", "txt": "document", "rst": "document",
        "json": "data", "yaml": "data", "yml": "data", "toml": "data",
        "html": "document", "tex": "document",
    }
    kinds = {_kind_map.get(e, "artifact") for e in ext_kinds}
    source_kind = kinds.pop() if len(kinds) == 1 else "artifact"

    raw_max_chars = spec.get("max_chars")
    try:
        prompt_max_chars: int | None = (
            int(raw_max_chars) if raw_max_chars is not None else None
        )
    except (TypeError, ValueError):
        prompt_max_chars = None
    prompt = _build_prompt(
        name, criterion, source, source_kind, max_chars=prompt_max_chars
    )
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
