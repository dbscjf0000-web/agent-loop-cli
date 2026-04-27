"""v0.3 Strategy Engine — multi-strategy plan fan-out + Selector.

Drives N parallel plan calls (cross-vendor recommended) and picks one winner
to feed downstream phases (Implement / Verify / Judge). Used by
``workers.run_plan`` when ``config.runtime.strategies`` is non-empty.

Like JudgeEngine the engine is a leaf component with no cross-call state. The
``_call_one`` pattern (clone Config, override ``cfg.models.plan``, dispatch to
``models.call_model``) is duplicated here on purpose — engine-to-engine import
would couple two independent fan-out implementations. If a third fan-out shows
up in v0.3.1, factor a shared ``_fanout.py`` module.

Selector v0.3.0 (deterministic, single-LLM, structural fallback)::

  - structural score per proposal:
        0.30 * length     (clamp 200..4000 chars, normalized)
        0.25 * fenced     (>=1 ```...``` block? 1.0 else 0.0)
        0.25 * steps      (numbered ``^\\d+\\.`` lines, log-scaled, capped)
        0.20 * headers    (``^#`` heading count, log-scaled, capped)
  - LLM rubric: one ``cfg.models.plan`` call asking which proposal is most
    actionable. JSON ``{winner_index, reason, scores: list[float]}``. On any
    failure the LLM contribution is dropped and structural-only ranking wins.
  - final score = 0.6 * llm + 0.4 * structural (when LLM succeeded)
                = structural (when LLM failed) → ``selector_method='fallback'``
  - tie-break: higher ``StrategySpec.weight``, then lower input index.

Failure modes::

  - one strategy fails    -> recorded as PlanProposal with ``error`` set.
                             Remaining proposals run through the selector.
  - every strategy fails  -> ``AllStrategiesFailed`` raised. Caller decides
                             between hard-fail or single-call fallback.
  - selector LLM fails    -> structural fallback (selector_method='fallback').
  - exactly 1 proposal    -> winner is that proposal. Selector not invoked.
"""
from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from agent_loop.config import Config, StrategySpec
from agent_loop.models import ModelResponse, call_model
from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PlanProposal:
    """One strategy's plan output. ``error`` set means the call failed."""

    provider: str
    weight: float
    text: str
    cost_usd: float = 0.0
    latency_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


@dataclass
class SelectionResult:
    """Outcome of running selector over N PlanProposals."""

    winner_index: int
    winner: PlanProposal
    proposals: list[PlanProposal]
    scores: list[dict[str, Any]] = field(default_factory=list)
    selector_method: str = "single"  # single | heuristic_only | heuristic+llm | fallback
    selector_error: str | None = None
    selector_reason: str = ""


class AllStrategiesFailed(RuntimeError):
    """Raised by StrategyEngine.fanout when every strategy errored."""

    def __init__(self, proposals: list[PlanProposal]) -> None:
        self.proposals = proposals
        msg = "; ".join(f"{p.provider}: {p.error}" for p in proposals)
        super().__init__(f"all {len(proposals)} strategies failed: {msg}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_STEP_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction (mirrors workers / judge_engine)."""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty response")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    a = s.find("{")
    b = s.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(s[a : b + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"could not parse JSON (len={len(s)})")


_SYSTEM_PROMPT = (
    "You are a focused, sober software engineer. Follow instructions exactly. "
    "Prefer correctness over cleverness. Output only what the prompt asks for."
)

_SELECTOR_SYSTEM = (
    "You are a senior engineering reviewer. Compare candidate plans and pick "
    "the most actionable, concrete, and complete one. Respond with JSON only."
)


# ---------------------------------------------------------------------------
# scoring (LLM-free)
# ---------------------------------------------------------------------------


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _score_heuristic(text: str) -> dict[str, float]:
    """Cheap structural quality estimate. All sub-scores in [0, 1].

    Weights chosen so a proposal that is well-structured (length + fenced code
    + numbered steps + headers) lands in the 0.7..0.9 band; a one-liner sits
    near 0.0; a wall-of-text without structure stays in the 0.3..0.5 band.
    """
    if not text:
        return {"length": 0.0, "fenced": 0.0, "steps": 0.0, "headers": 0.0, "structural": 0.0}

    n = len(text)
    # length: clamp 200..4000, normalize. Anything below 200 chars feels too short;
    # anything above 4000 stops adding signal (could be padding).
    length = _clamp((n - 200) / (4000 - 200), 0.0, 1.0)

    fenced = 1.0 if _CODE_FENCE_RE.search(text) else 0.0

    step_count = len(_STEP_RE.findall(text))
    # log-scaled cap at 8 steps (≈ enough for any reasonable plan).
    steps = _clamp(math.log1p(step_count) / math.log1p(8), 0.0, 1.0)

    header_count = len(_HEADER_RE.findall(text))
    headers = _clamp(math.log1p(header_count) / math.log1p(6), 0.0, 1.0)

    structural = 0.30 * length + 0.25 * fenced + 0.25 * steps + 0.20 * headers
    return {
        "length": round(length, 4),
        "fenced": round(fenced, 4),
        "steps": round(steps, 4),
        "headers": round(headers, 4),
        "structural": round(structural, 4),
    }


# ---------------------------------------------------------------------------
# StrategyEngine
# ---------------------------------------------------------------------------


class StrategyEngine:
    """Fan out a plan prompt to N strategies and pick a winner.

    Constructed once per ``run_plan`` call. The engine reads ``Config`` only
    to clone it for per-strategy ``models.plan`` overrides; it never mutates
    the config it was handed.
    """

    def __init__(self, task_dir: TaskDir, config: Config) -> None:
        self.task_dir = task_dir
        self.config = config

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def fanout(
        self,
        strategies: list[StrategySpec],
        prompt: str,
        *,
        cli_timeout: float = 600.0,
    ) -> SelectionResult:
        """Fan-out + selector. Returns a complete ``SelectionResult``.

        Single-proposal short-circuit: if exactly one spec is supplied **and
        the call succeeds**, the selector is skipped entirely (no LLM call,
        no structural scoring). If the single call fails, ``AllStrategiesFailed``
        is raised — caller picks the fallback policy.
        """
        if not strategies:
            raise ValueError("StrategyEngine.fanout called with empty strategies list")

        proposals = self._fan_out(strategies, prompt, cli_timeout=cli_timeout)

        valid = [p for p in proposals if p.error is None]
        if not valid:
            raise AllStrategiesFailed(proposals)

        if len(proposals) == 1 and proposals[0].error is None:
            # Single-strategy short-circuit: no selector cost.
            return SelectionResult(
                winner_index=0,
                winner=proposals[0],
                proposals=proposals,
                scores=[{"provider": proposals[0].provider, **_score_heuristic(proposals[0].text)}],
                selector_method="single",
                selector_reason="only one strategy supplied",
            )

        return self._select(proposals)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fan_out(
        self,
        strategies: list[StrategySpec],
        prompt: str,
        *,
        cli_timeout: float,
    ) -> list[PlanProposal]:
        """ThreadPool fan-out preserving input order in the returned list."""
        results: dict[int, PlanProposal] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(strategies))) as ex:
            futures = {
                ex.submit(self._call_one, spec, prompt, cli_timeout): idx
                for idx, spec in enumerate(strategies)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                spec = strategies[idx]
                try:
                    results[idx] = fut.result()
                except Exception as e:  # pragma: no cover - belt&suspenders
                    results[idx] = PlanProposal(
                        provider=spec.provider,
                        weight=spec.weight,
                        text="",
                        error=f"{type(e).__name__}: {e}"[:500],
                    )
        return [results[i] for i in range(len(strategies))]

    def _call_one(
        self,
        spec: StrategySpec,
        prompt: str,
        cli_timeout: float,
    ) -> PlanProposal:
        """Call a single strategy by overriding cfg.models.plan."""
        cfg = self.config.model_copy(deep=True)
        cfg.models.plan = spec.provider

        started = time.monotonic()
        try:
            resp: ModelResponse = call_model(
                "plan",
                prompt,
                system=_SYSTEM_PROMPT,
                config=cfg,
                workspace=self.task_dir.workspace_path(),
                cli_timeout=cli_timeout,
            )
        except Exception as e:
            return PlanProposal(
                provider=spec.provider,
                weight=spec.weight,
                text="",
                latency_s=time.monotonic() - started,
                error=f"{type(e).__name__}: {e}"[:500],
            )

        return PlanProposal(
            provider=spec.provider,
            weight=spec.weight,
            text=resp.text or "",
            cost_usd=float(resp.cost_usd or 0.0),
            latency_s=float(resp.latency_s or (time.monotonic() - started)),
            tokens_in=int(resp.prompt_tokens or 0),
            tokens_out=int(resp.completion_tokens or 0),
            error=None,
        )

    # ------------------------------------------------------------------
    # selector
    # ------------------------------------------------------------------

    def _select(self, proposals: list[PlanProposal]) -> SelectionResult:
        """Run heuristic + LLM rubric, blend, pick winner deterministically."""
        # 1. Structural score for every proposal (skip failed ones — score 0.0)
        scores: list[dict[str, Any]] = []
        for p in proposals:
            if p.error is not None:
                scores.append({
                    "provider": p.provider,
                    "weight": p.weight,
                    "structural": 0.0,
                    "llm": None,
                    "final": 0.0,
                    "error": p.error,
                })
            else:
                h = _score_heuristic(p.text)
                scores.append({
                    "provider": p.provider,
                    "weight": p.weight,
                    **h,
                    "llm": None,
                    "final": h["structural"],  # provisional, may be overwritten
                    "error": None,
                })

        # 2. LLM rubric (one call). On any failure -> structural-only fallback.
        valid_indices = [i for i, p in enumerate(proposals) if p.error is None]
        llm_scores: dict[int, float] | None = None
        selector_error: str | None = None
        selector_reason = ""

        if len(valid_indices) >= 2:
            try:
                llm_scores, selector_reason = self._llm_rubric(proposals, valid_indices)
            except Exception as e:
                selector_error = f"{type(e).__name__}: {e}"[:500]
                llm_scores = None

        if llm_scores is not None:
            # Blend: 0.6 LLM + 0.4 structural for valid proposals.
            for idx in valid_indices:
                s = scores[idx]
                llm_v = float(llm_scores.get(idx, 0.0))
                s["llm"] = round(llm_v, 4)
                s["final"] = round(0.6 * llm_v + 0.4 * float(s["structural"]), 4)
            method = "heuristic+llm"
        else:
            method = "fallback" if selector_error else "heuristic_only"

        # 3. Pick the winner: highest final, tie-break = higher weight, lower index.
        # Failed proposals have final=0 with weight unaffected; they will only win
        # if every valid proposal also has final=0 (extremely unlikely in practice
        # but kept defensive). To be safe, restrict winner to valid_indices first.
        candidates = valid_indices or list(range(len(proposals)))

        def _key(idx: int) -> tuple[float, float, int]:
            s = scores[idx]
            return (-float(s["final"]), -float(s.get("weight", 1.0)), idx)

        candidates.sort(key=_key)
        winner_index = candidates[0]
        winner = proposals[winner_index]

        return SelectionResult(
            winner_index=winner_index,
            winner=winner,
            proposals=proposals,
            scores=scores,
            selector_method=method,
            selector_error=selector_error,
            selector_reason=selector_reason,
        )

    def _llm_rubric(
        self,
        proposals: list[PlanProposal],
        valid_indices: list[int],
    ) -> tuple[dict[int, float], str]:
        """Single LLM call to rank proposals. Returns (scores_by_index, reason).

        Raises any exception from ``call_model`` or JSON parsing — caller
        catches and falls back to structural-only.
        """
        # Build a compact rubric prompt. Each proposal capped at 4000 chars to
        # keep the rubric call cheap regardless of plan verbosity.
        cap = 4000
        parts: list[str] = []
        parts.append(
            "Below are candidate PLANS for the same task. Rank them by how "
            "actionable, concrete, and complete they are. Score each in [0, 1] "
            "(1 = best). Return JSON only:"
        )
        parts.append("")
        parts.append("```json")
        parts.append('{"winner_index": <int>, "reason": "<string>", "scores": [<float per proposal in input order>]}')
        parts.append("```")
        parts.append("")
        for i, p in enumerate(proposals):
            label = f"--- proposal #{i} (provider={p.provider})"
            if p.error is not None:
                parts.append(f"{label} [ERRORED: {p.error[:120]}]")
                parts.append("(no text)")
            else:
                parts.append(label)
                parts.append((p.text or "")[:cap])
            parts.append("")

        prompt = "\n".join(parts)

        # Use the configured plan model for the rubric (same provider used for
        # the planning, so authentication and pricing already work).
        cfg = self.config.model_copy(deep=True)
        # Note: cfg.models.plan stays at the original (unmodified by callers
        # of fanout — _call_one mutates only its own clone).
        resp = call_model(
            "plan",
            prompt,
            system=_SELECTOR_SYSTEM,
            config=cfg,
            workspace=self.task_dir.workspace_path(),
        )
        parsed = _extract_json(resp.text)

        raw_scores = parsed.get("scores")
        if not isinstance(raw_scores, list):
            raise ValueError("rubric response missing 'scores' list")

        if len(raw_scores) != len(proposals):
            raise ValueError(
                f"rubric scores length mismatch ({len(raw_scores)} vs {len(proposals)})"
            )

        scores_by_index: dict[int, float] = {}
        for i in valid_indices:
            try:
                v = float(raw_scores[i])
            except (TypeError, ValueError):
                v = 0.0
            scores_by_index[i] = _clamp(v, 0.0, 1.0)

        reason = str(parsed.get("reason") or "").strip()
        return scores_by_index, reason


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


def proposal_to_dict(p: PlanProposal) -> dict[str, Any]:
    return asdict(p)


def selection_to_dict(s: SelectionResult) -> dict[str, Any]:
    return {
        "winner_index": s.winner_index,
        "winner_provider": s.winner.provider,
        "selector_method": s.selector_method,
        "selector_error": s.selector_error,
        "selector_reason": s.selector_reason,
        "scores": s.scores,
    }


__all__ = [
    "AllStrategiesFailed",
    "PlanProposal",
    "SelectionResult",
    "StrategyEngine",
    "proposal_to_dict",
    "selection_to_dict",
]
