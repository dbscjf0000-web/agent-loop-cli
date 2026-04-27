"""Configuration loading for agent-loop-cli.

Priority (highest first):
1. Explicit path passed to load_config()
2. ./agent-loop.toml in current working directory
3. ~/.agent-loop/config.toml
4. Built-in defaults

Environment variables can override individual fields:
  AGENT_LOOP_MODEL_RESEARCH, AGENT_LOOP_MODEL_PLAN, AGENT_LOOP_MODEL_IMPLEMENT,
  AGENT_LOOP_MODEL_VERIFY, AGENT_LOOP_MODEL_JUDGE
  AGENT_LOOP_BUDGET_DAILY_USD, AGENT_LOOP_BUDGET_PER_RUN_USD
  AGENT_LOOP_RUNTIME_SANDBOX, AGENT_LOOP_RUNTIME_MAX_CYCLES, AGENT_LOOP_RUNTIME_MAX_REDO
  AGENT_LOOP_RUNTIME_JUDGES        (v0.3, comma-sep providers, weight=1.0 each)
  AGENT_LOOP_RUNTIME_STRATEGIES    (v0.3, comma-sep providers, weight=1.0 each)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT             (v0.3.1, default subprocess timeout — seconds)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT_RESEARCH    (v0.3.1, per-phase override)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT_PLAN        (v0.3.1, per-phase override)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT_IMPLEMENT   (v0.3.1, per-phase override)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT_VERIFY      (v0.3.1, per-phase override)
  AGENT_LOOP_RUNTIME_CLI_TIMEOUT_JUDGE       (v0.3.1, per-phase override)
  AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM        (v0.3.1, bool — disable first-cycle short-circuit)
  AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY            (v0.4, bool — enable cross-task patterns.md)
  AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR        (v0.4, override ~/.agent-loop/global)
  AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_MAX_CHARS  (v0.4, int — snapshot slice budget)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as _toml


DEFAULT_USER_CONFIG = Path.home() / ".agent-loop" / "config.toml"
PROJECT_CONFIG_NAME = "agent-loop.toml"


class Models(BaseModel):
    research: str = "anthropic/claude-opus-4-7"
    plan: str = "anthropic/claude-opus-4-7"
    implement: str = "anthropic/claude-sonnet-4-6"
    verify: str = "anthropic/claude-haiku-4-5"
    judge: str = "openai/gpt-5.2"


class Budget(BaseModel):
    daily_usd: float = 10.0
    per_run_usd: float = 2.0


class JudgeSpec(BaseModel):
    """v0.3 multi-judge entry — one provider + weight."""

    provider: str
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def _weight_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"judge weight must be > 0 (got {v})")
        return float(v)


class StrategySpec(BaseModel):
    """v0.3 multi-strategy plan entry — one provider + weight (selector tie-break)."""

    provider: str
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def _weight_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"strategy weight must be > 0 (got {v})")
        return float(v)


class Runtime(BaseModel):
    sandbox: bool = True
    max_cycles: int = 10
    max_redo: int = 3
    # v0.3 multi-judge consensus. None / empty list -> single-judge mode.
    judges: list[JudgeSpec] | None = None
    # v0.3 multi-strategy plan fan-out. None / empty list -> single-plan mode.
    strategies: list[StrategySpec] | None = None

    # v0.3.1 — subprocess timeout (seconds) for CLI providers. ``cli_timeout``
    # is the default applied to every phase; a per-phase override (e.g.
    # ``cli_timeout_verify``) takes precedence when set. ``None`` means "use
    # the default". See :py:meth:`Runtime.cli_timeout_for`.
    cli_timeout: int = 600
    cli_timeout_research: int | None = None
    cli_timeout_plan: int | None = None
    cli_timeout_implement: int | None = None
    cli_timeout_verify: int | None = None
    cli_timeout_judge: int | None = None

    # v0.3.1 — when True, disable the judge's first-cycle short-circuit and
    # invoke the LLM (or multi-judge consensus) even on cycle 1 (where there
    # is no ``best_solution.json`` to compare against). Useful for live
    # cross-vendor multi-judge verification when score>=0.95 is reached on
    # cycle 1 (which would otherwise auto-stop without any judge LLM call).
    judge_always_llm: bool = False

    # v0.4 — cross-task memory (per-user global learning).
    # When True (default), ContextEngine.snapshot() includes a slice of
    # ``<cross_task_memory_dir>/patterns.md`` and the orchestrator commits
    # this task's CORE: lines + a one-line summary at run end. Set to False
    # to revert to v0.3 single-task-only memory.
    cross_task_memory: bool = True
    cross_task_memory_dir: str = "~/.agent-loop/global"
    cross_task_memory_max_chars: int = 4000

    def cli_timeout_for(self, phase: str) -> int:
        """Return the effective subprocess timeout (seconds) for ``phase``.

        Per-phase overrides win when set; otherwise the runtime-wide
        ``cli_timeout`` default is used. Unknown phase names fall back to the
        default (defensive — this method is called from a string literal in
        :py:func:`agent_loop.models.call_model`).
        """
        override = getattr(self, f"cli_timeout_{phase}", None)
        if override is not None:
            return int(override)
        return int(self.cli_timeout)

    @field_validator("judges", mode="before")
    @classmethod
    def _normalize_judges(cls, v: Any) -> Any:
        """Accept three input shapes:

        - None / missing               -> None (single-judge mode)
        - list[str]                    -> [JudgeSpec(provider=s, weight=1.0)]
        - list[dict] / list[JudgeSpec] -> passed through, validated below
        - empty list                   -> None (single-judge mode)
        """
        if v is None:
            return None
        if not isinstance(v, list):
            raise TypeError("runtime.judges must be a list")
        if not v:
            return None
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"provider": item, "weight": 1.0})
            else:
                out.append(item)
        return out

    @field_validator("strategies", mode="before")
    @classmethod
    def _normalize_strategies(cls, v: Any) -> Any:
        """Same normalization as ``_normalize_judges`` but for strategies."""
        if v is None:
            return None
        if not isinstance(v, list):
            raise TypeError("runtime.strategies must be a list")
        if not v:
            return None
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"provider": item, "weight": 1.0})
            else:
                out.append(item)
        return out


class Config(BaseModel):
    models: Models = Field(default_factory=Models)
    budget: Budget = Field(default_factory=Budget)
    runtime: Runtime = Field(default_factory=Runtime)


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return _toml.load(f)


def _resolve_config_path(explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        if not explicit_path.exists():
            raise FileNotFoundError(f"Config not found: {explicit_path}")
        return explicit_path

    project = Path.cwd() / PROJECT_CONFIG_NAME
    if project.exists():
        return project

    if DEFAULT_USER_CONFIG.exists():
        return DEFAULT_USER_CONFIG

    return None


_ENV_MAP = {
    ("models", "research"): ("AGENT_LOOP_MODEL_RESEARCH", str),
    ("models", "plan"): ("AGENT_LOOP_MODEL_PLAN", str),
    ("models", "implement"): ("AGENT_LOOP_MODEL_IMPLEMENT", str),
    ("models", "verify"): ("AGENT_LOOP_MODEL_VERIFY", str),
    ("models", "judge"): ("AGENT_LOOP_MODEL_JUDGE", str),
    ("budget", "daily_usd"): ("AGENT_LOOP_BUDGET_DAILY_USD", float),
    ("budget", "per_run_usd"): ("AGENT_LOOP_BUDGET_PER_RUN_USD", float),
    ("runtime", "max_cycles"): ("AGENT_LOOP_RUNTIME_MAX_CYCLES", int),
    ("runtime", "max_redo"): ("AGENT_LOOP_RUNTIME_MAX_REDO", int),
    ("runtime", "sandbox"): ("AGENT_LOOP_RUNTIME_SANDBOX", "bool"),
    # v0.3.1 cli_timeout (default + per-phase overrides)
    ("runtime", "cli_timeout"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT", int),
    ("runtime", "cli_timeout_research"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_RESEARCH", int),
    ("runtime", "cli_timeout_plan"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_PLAN", int),
    ("runtime", "cli_timeout_implement"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_IMPLEMENT", int),
    ("runtime", "cli_timeout_verify"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_VERIFY", int),
    ("runtime", "cli_timeout_judge"): ("AGENT_LOOP_RUNTIME_CLI_TIMEOUT_JUDGE", int),
    # v0.3.1 judge_always_llm
    ("runtime", "judge_always_llm"): ("AGENT_LOOP_RUNTIME_JUDGE_ALWAYS_LLM", "bool"),
    # v0.4 cross-task memory
    ("runtime", "cross_task_memory"): ("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY", "bool"),
    ("runtime", "cross_task_memory_dir"): ("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str),
    ("runtime", "cross_task_memory_max_chars"): ("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_MAX_CHARS", int),
}


def _coerce(raw: str, kind: Any) -> Any:
    if kind == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return kind(raw)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for (section, key), (env_name, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        data.setdefault(section, {})[key] = _coerce(raw, kind)

    # v0.3 multi-judge: comma-separated provider list -> [{provider, weight=1}, ...]
    raw_j = os.environ.get("AGENT_LOOP_RUNTIME_JUDGES")
    if raw_j is not None:
        items = [s.strip() for s in raw_j.split(",") if s.strip()]
        if items:
            data.setdefault("runtime", {})["judges"] = [
                {"provider": s, "weight": 1.0} for s in items
            ]
        else:
            # explicit empty -> disable multi-judge
            data.setdefault("runtime", {})["judges"] = None

    # v0.3 multi-strategy: comma-separated provider list (parallel to judges).
    raw_s = os.environ.get("AGENT_LOOP_RUNTIME_STRATEGIES")
    if raw_s is not None:
        items = [s.strip() for s in raw_s.split(",") if s.strip()]
        if items:
            data.setdefault("runtime", {})["strategies"] = [
                {"provider": s, "weight": 1.0} for s in items
            ]
        else:
            data.setdefault("runtime", {})["strategies"] = None
    return data


def load_config(explicit_path: Path | None = None) -> Config:
    """Load Config with file + env-var override layering."""
    path = _resolve_config_path(explicit_path)
    data: dict[str, Any] = _read_toml(path) if path is not None else {}
    data = _apply_env_overrides(data)
    return Config.model_validate(data)


_DEFAULT_TOML = """\
# agent-loop-cli config

[models]
research  = "anthropic/claude-opus-4-7"
plan      = "anthropic/claude-opus-4-7"
implement = "anthropic/claude-sonnet-4-6"
verify    = "anthropic/claude-haiku-4-5"
judge     = "openai/gpt-5.2"

[budget]
daily_usd   = 10
per_run_usd = 2

[runtime]
sandbox     = true
max_cycles  = 10
max_redo    = 3

# v0.3.1 - subprocess timeout (seconds) for CLI providers. `cli_timeout`
# is the default applied to every phase; per-phase overrides win when set.
cli_timeout         = 600
# cli_timeout_research = 600
# cli_timeout_plan     = 600
# cli_timeout_implement= 600
# cli_timeout_verify   = 900
# cli_timeout_judge    = 180

# v0.3.1 - when true, disable the judge's first-cycle short-circuit and
# always invoke the LLM (single or multi-judge consensus) even on cycle 1.
judge_always_llm    = false

# v0.4 - cross-task memory: persist CORE: lines + one-line task summaries
# under <cross_task_memory_dir>/ so future tasks see prior learning.
# Set to false to disable (reverts to v0.3 single-task memory).
cross_task_memory               = true
cross_task_memory_dir           = "~/.agent-loop/global"
cross_task_memory_max_chars     = 4000
"""


def init_default_config(path: Path | None = None) -> Path:
    """Write a default config to `path` (default: ~/.agent-loop/config.toml).

    Returns the path written. Refuses to overwrite an existing file.
    """
    target = path or DEFAULT_USER_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")
    target.write_text(_DEFAULT_TOML, encoding="utf-8")
    return target
