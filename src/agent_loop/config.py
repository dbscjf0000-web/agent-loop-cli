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
