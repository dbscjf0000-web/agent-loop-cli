"""v0.3 Judge Engine — multi-judge consensus.

Drives N parallel judge calls (cross-vendor recommended) and aggregates the
results via weighted majority + weighted-average scoring. Used by
``workers.run_judge`` when ``config.runtime.judges`` is non-empty.

The engine is intentionally a leaf component: it owns no state across calls,
does not touch the orchestrator, and shares only the prompt template + the
``models.call_model`` dispatch with the rest of the package. CLI subprocess
calls (cursor / claude / gemini) are IO bound, so a small ThreadPoolExecutor
is the right tool from stdlib (no new deps).

Aggregation rules (deterministic):
  - action:    weighted majority on (stop / redo_R / redo_P). Tie -> ``stop``
               (conservative); if ``stop`` not in tie -> alphabetic first.
  - better:    weighted true/false sum. ``true_w > false_w`` -> True
               (conservative on tie -> False).
  - score:     weighted average of judges that reported a score; None if all
               omitted.
  - hint:      ``\\n---\\n`` concat of non-empty hints.
  - reason:    ``[provider] reason`` concat with same separator.

Failure modes:
  - one judge timeout / error -> recorded as an IndividualJudgement with
    ``error`` set, the others continue (partial consensus).
  - every judge fails -> ``AllJudgesFailed`` raised, caller (workers.run_judge)
    falls back to ``_run_judge_single`` with ``consensus.fallback=True``
    annotation.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from agent_loop.config import Config, JudgeSpec
from agent_loop.models import ModelResponse, call_model
from agent_loop.state import TaskDir


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IndividualJudgement:
    """One judge's vote. ``error`` set means the call failed."""

    provider: str
    weight: float
    better: bool
    action: str
    weighted_score: float | None
    hint: str
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_s: float = 0.0


@dataclass
class ConsensusResult:
    """Aggregate of N IndividualJudgement votes."""

    better: bool
    action: str
    scores: dict[str, Any]
    hint: str
    reason: str
    individual: list[IndividualJudgement]
    n_judges: int
    votes_action: dict[str, float]
    votes_better: dict[str, float]
    fallback: bool = False  # True iff every judge failed and caller used single


class AllJudgesFailed(RuntimeError):
    """Raised by JudgeEngine.consensus when every judge call errored."""

    def __init__(self, individuals: list[IndividualJudgement]) -> None:
        self.individuals = individuals
        msg = "; ".join(f"{i.provider}: {i.error}" for i in individuals)
        super().__init__(f"all {len(individuals)} judges failed: {msg}")


# ---------------------------------------------------------------------------
# helpers (small copies of workers' helpers — keeps engine self-contained)
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction (mirrors workers._extract_json)."""
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


def _load_judge_prompt() -> str:
    """Read prompts/judge.md from the installed package (or src tree)."""
    pkg = "agent_loop.prompts"
    try:
        return resources.files(pkg).joinpath("judge.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return (Path(__file__).parent / "prompts" / "judge.md").read_text(encoding="utf-8")


_VALID_ACTIONS = {"stop", "redo_R", "redo_P"}


def _normalize_action(raw: Any) -> str:
    if not isinstance(raw, str):
        return "stop"
    s = raw.strip()
    return s if s in _VALID_ACTIONS else "stop"


def _extract_score(parsed: dict[str, Any]) -> float | None:
    """Pull a single judge's reported score (this_cycle preferred, else weighted_score)."""
    scores = parsed.get("scores")
    if isinstance(scores, dict):
        v = scores.get("this_cycle")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    v = parsed.get("weighted_score")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# JudgeEngine
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a focused, sober software engineer. Follow instructions exactly. "
    "Prefer correctness over cleverness. Output only what the prompt asks for."
)


class JudgeEngine:
    """Run N judges in parallel and aggregate their votes.

    Constructed once per ``run_judge`` call; not meant to be reused across
    cycles (state on disk is the source of truth).
    """

    def __init__(self, task_dir: TaskDir, config: Config) -> None:
        self.task_dir = task_dir
        self.config = config

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def consensus(
        self,
        judges: list[JudgeSpec],
        prompt: str,
        *,
        cli_timeout: float = 600.0,
    ) -> ConsensusResult:
        """Fan out the same `prompt` to every JudgeSpec and aggregate."""
        if not judges:
            raise ValueError("JudgeEngine.consensus called with empty judges list")

        individuals = self._fan_out(judges, prompt, cli_timeout=cli_timeout)
        return self._aggregate(individuals)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fan_out(
        self,
        judges: list[JudgeSpec],
        prompt: str,
        *,
        cli_timeout: float,
    ) -> list[IndividualJudgement]:
        """ThreadPool fan-out. Returns one IndividualJudgement per spec, in input order."""
        # Dict by index so we preserve input order in the returned list even
        # though as_completed yields out of order.
        results: dict[int, IndividualJudgement] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(judges))) as ex:
            futures = {
                ex.submit(self._call_one, spec, prompt, cli_timeout): idx
                for idx, spec in enumerate(judges)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                spec = judges[idx]
                try:
                    results[idx] = fut.result()
                except Exception as e:  # pragma: no cover - belt&suspenders
                    results[idx] = IndividualJudgement(
                        provider=spec.provider,
                        weight=spec.weight,
                        better=False,
                        action="stop",
                        weighted_score=None,
                        hint="",
                        reason="",
                        raw={},
                        error=f"{type(e).__name__}: {e}"[:500],
                    )
        return [results[i] for i in range(len(judges))]

    def _call_one(
        self,
        spec: JudgeSpec,
        prompt: str,
        cli_timeout: float,
    ) -> IndividualJudgement:
        """Call a single judge by overriding cfg.models.judge for this spec."""
        # Clone config so we don't mutate shared state across threads.
        cfg = self.config.model_copy(deep=True)
        cfg.models.judge = spec.provider

        started = time.monotonic()
        try:
            resp: ModelResponse = call_model(
                "judge",
                prompt,
                system=_SYSTEM_PROMPT,
                config=cfg,
                workspace=self.task_dir.workspace_path(),
                cli_timeout=cli_timeout,
            )
        except Exception as e:
            return IndividualJudgement(
                provider=spec.provider,
                weight=spec.weight,
                better=False,
                action="stop",
                weighted_score=None,
                hint="",
                reason="",
                raw={},
                error=f"{type(e).__name__}: {e}"[:500],
                latency_s=time.monotonic() - started,
            )

        latency = time.monotonic() - started
        try:
            parsed = _extract_json(resp.text)
        except ValueError as e:
            return IndividualJudgement(
                provider=spec.provider,
                weight=spec.weight,
                better=False,
                action="stop",
                weighted_score=None,
                hint="",
                reason="",
                raw={"_raw_text": (resp.text or "")[:1000]},
                error=f"unparseable JSON: {e}",
                latency_s=latency,
            )

        return IndividualJudgement(
            provider=spec.provider,
            weight=spec.weight,
            better=bool(parsed.get("better", False)),
            action=_normalize_action(parsed.get("action")),
            weighted_score=_extract_score(parsed),
            hint=str(parsed.get("hint") or ""),
            reason=str(parsed.get("reason") or ""),
            raw=parsed,
            error=None,
            latency_s=latency,
        )

    def _aggregate(self, individuals: list[IndividualJudgement]) -> ConsensusResult:
        """Apply the deterministic majority + weighted-avg rules."""
        valid = [i for i in individuals if i.error is None]
        if not valid:
            raise AllJudgesFailed(individuals)

        # action: weighted majority. Tie -> 'stop' if present, else alphabetic first.
        votes_action: dict[str, float] = {}
        for j in valid:
            votes_action[j.action] = votes_action.get(j.action, 0.0) + j.weight
        max_w = max(votes_action.values())
        top_actions = sorted(a for a, w in votes_action.items() if w == max_w)
        if "stop" in top_actions:
            action = "stop"
        else:
            action = top_actions[0]

        # better: weighted true vs false. Tie -> False (conservative).
        votes_better: dict[str, float] = {"true": 0.0, "false": 0.0}
        for j in valid:
            votes_better["true" if j.better else "false"] += j.weight
        better = votes_better["true"] > votes_better["false"]

        # weighted score average over judges that reported a score.
        weighted_avg: float | None = None
        scored = [(j.weight, j.weighted_score) for j in valid if j.weighted_score is not None]
        if scored:
            total_w = sum(w for w, _ in scored)
            if total_w > 0:
                weighted_avg = sum(w * s for w, s in scored) / total_w  # type: ignore[operator]

        # concat hint / reason for prompt re-injection next cycle
        hint = "\n---\n".join(j.hint for j in valid if j.hint)
        reason = "\n---\n".join(
            f"[{j.provider}] {j.reason}" for j in valid if j.reason
        )

        scores: dict[str, Any] = {"weighted": weighted_avg}

        return ConsensusResult(
            better=better,
            action=action,
            scores=scores,
            hint=hint,
            reason=reason,
            individual=individuals,
            n_judges=len(individuals),
            votes_action=votes_action,
            votes_better=votes_better,
            fallback=False,
        )


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


def individual_to_dict(j: IndividualJudgement) -> dict[str, Any]:
    """Drop ``raw`` (might be huge) when persisting to judge_result.json."""
    d = asdict(j)
    # keep raw in a side artifact only if needed; here we slim it down
    raw = d.pop("raw", {})
    if isinstance(raw, dict) and raw.get("_raw_text"):
        d["raw_text"] = raw["_raw_text"][:500]
    return d


def consensus_to_dict(c: ConsensusResult) -> dict[str, Any]:
    """Render ConsensusResult into the v0.3 ``consensus`` payload."""
    return {
        "n_judges": c.n_judges,
        "votes_action": c.votes_action,
        "votes_better": c.votes_better,
        "fallback": c.fallback,
        "individual": [individual_to_dict(i) for i in c.individual],
    }


__all__ = [
    "AllJudgesFailed",
    "ConsensusResult",
    "IndividualJudgement",
    "JudgeEngine",
    "consensus_to_dict",
    "individual_to_dict",
]
