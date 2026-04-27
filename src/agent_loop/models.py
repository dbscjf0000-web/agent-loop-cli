"""Thin litellm wrapper that maps a phase name to a configured model.

Four provider paths are supported:
  - litellm (default): any `provider/model` recognized by litellm.
  - cursor-agent CLI: ids of the form `cursor` or `cursor/<model>`,
    invoked via the locally installed `cursor-agent` binary in `--print` mode.
  - Claude Code CLI: ids of the form `claude` or `claude/<model>`,
    invoked via the locally installed `claude` binary in `--print` mode
    (uses the user's logged-in Anthropic account, no API key needed).
  - Gemini CLI: ids of the form `gemini` or `gemini/<model>` (e.g.
    `gemini/gemini-2.5-pro`), invoked via the local `gemini` binary in
    `-p` headless mode (uses the user's oauth-personal account).

All CLI providers are themselves agents — a single call may use tools,
edit files, and take tens of seconds to several minutes.

Example:
    from agent_loop.config import load_config
    from agent_loop.models import call_model

    cfg = load_config()
    resp = call_model("research", "Summarize the task.", config=cfg)
    print(resp.text, resp.cost_usd, resp.latency_s)
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import litellm

from agent_loop.config import Config, load_config

Phase = Literal["research", "plan", "implement", "verify", "judge"]


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float
    model: str


@dataclass
class CodeAndContext:
    code: str
    task: str
    plan: str
    findings: str
    execution_log: str


@dataclass
class CodeAnalysis:
    summary: str
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


def _model_for_phase(phase: Phase, cfg: Config) -> str:
    return getattr(cfg.models, phase)


def _extract_text(response: Any) -> str:
    # litellm mirrors OpenAI's chat.completions schema.
    try:
        return response.choices[0].message.content or ""
    except AttributeError:
        # dict-style fallback (mocks, raw JSON)
        return response["choices"][0]["message"]["content"] or ""


def _extract_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None) or (response.get("usage") if isinstance(response, dict) else None)
    if not usage:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


def _safe_cost(response: Any) -> float:
    # litellm.completion_cost can fail for unknown models / mocked responses.
    try:
        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# cursor-agent CLI provider
# ---------------------------------------------------------------------------

def _is_cursor_model(model: str) -> bool:
    if not model:
        return False
    return model == "cursor" or model.startswith("cursor/")


def _cursor_model_arg(model: str) -> str:
    """Translate a config string like `cursor/sonnet-4` into the `--model` value.

    `cursor` alone -> `auto`. Otherwise, strip the `cursor/` prefix.
    """
    if model == "cursor":
        return "auto"
    return model.split("/", 1)[1] if "/" in model else "auto"


def _call_cursor_cli(
    prompt: str,
    system: str = "",
    *,
    model: str = "auto",
    workspace: Path | str | None = None,
    timeout: float = 600.0,
) -> ModelResponse:
    """Invoke the local `cursor-agent` CLI in non-interactive mode.

    Parameters
    ----------
    prompt:
        Task / user message.
    system:
        Optional system-style preamble. cursor-agent has no separate system slot,
        so we render the two as `# System\\n\\n<sys>\\n\\n# Task\\n\\n<prompt>`.
    model:
        cursor-agent model id (e.g. ``auto`` / ``sonnet-4`` / ``gpt-5``).
    workspace:
        Directory to run the agent in (mapped to ``--workspace``). When omitted
        cursor-agent uses the current working directory.
    timeout:
        Hard subprocess timeout (seconds).

    Returns
    -------
    ModelResponse
        ``cost_usd`` is always 0.0 (Pro subscription assumed). Token counts are
        a rough char/4 estimate — cursor-agent does not surface real usage.
    """
    cli = shutil.which("cursor-agent")
    if cli is None:
        raise RuntimeError(
            "cursor-agent CLI not found on PATH. "
            "Install from https://cursor.com or run `cursor-agent login` first."
        )

    rendered = (
        f"# System\n\n{system.strip()}\n\n# Task\n\n{prompt.strip()}"
        if system.strip()
        else prompt
    )

    cmd: list[str] = [
        cli,
        "--print",
        "--output-format",
        "text",
        "--force",
        "--trust",
        "--model",
        model,
    ]
    if workspace is not None:
        cmd.extend(["--workspace", str(workspace)])
    cmd.append(rendered)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"cursor-agent timed out after {timeout:.0f}s "
            f"(model={model}, workspace={workspace})"
        ) from e
    latency_s = time.monotonic() - started

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(
            f"cursor-agent failed (rc={proc.returncode}, model={model}): {stderr_tail}"
        )

    text = (proc.stdout or "").rstrip("\n")
    return ModelResponse(
        text=text,
        prompt_tokens=max(1, len(rendered) // 4),
        completion_tokens=max(0, len(text) // 4),
        cost_usd=0.0,
        latency_s=latency_s,
        model=f"cursor/{model}",
    )


# ---------------------------------------------------------------------------
# Claude Code CLI provider
# ---------------------------------------------------------------------------

def _is_claude_model(model: str) -> bool:
    if not model:
        return False
    return model == "claude" or model.startswith("claude/")


def _claude_model_arg(model: str) -> str:
    """Translate a config string like `claude/default` into the model name.

    Claude Code CLI does not currently expose a `--model` flag from the wrapper —
    it picks model based on its settings. We retain the suffix for forward
    compatibility (v0.3+ may pass it via env or settings); for now it is purely
    informational and surfaced in the ModelResponse.model field.
    """
    if model == "claude":
        return "default"
    return model.split("/", 1)[1] if "/" in model else "default"


def _call_claude_cli(
    prompt: str,
    system: str = "",
    *,
    model: str = "default",
    workspace: Path | str | None = None,
    timeout: float = 600.0,
) -> ModelResponse:
    """Invoke the local `claude` CLI (Claude Code) in non-interactive mode.

    Uses ``--print --output-format text --dangerously-skip-permissions
    --allowedTools=NoneSuch``. The phantom tool name forces Claude Code into
    plain-LLM mode (no tool calls, no agentic self-invoke) so verify/judge
    prompts return in seconds instead of minutes; ``--add-dir`` brings the
    workspace into reach for context.

    The user's Claude Code login is reused — no API key required.
    cost_usd is reported as 0 because subscription billing is opaque.
    """
    cli = shutil.which("claude")
    if cli is None:
        raise RuntimeError(
            "claude CLI not found on PATH. "
            "Install Claude Code from https://claude.com/code or run `claude` once to log in."
        )

    rendered = (
        f"# System\n\n{system.strip()}\n\n# Task\n\n{prompt.strip()}"
        if system.strip()
        else prompt
    )

    cmd: list[str] = [
        cli,
        "--print",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
        "--allowedTools=NoneSuch",
    ]
    if workspace is not None:
        cmd.extend([f"--add-dir={workspace}"])
    cmd.append(rendered)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"claude CLI timed out after {timeout:.0f}s "
            f"(model={model}, workspace={workspace})"
        ) from e
    latency_s = time.monotonic() - started

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(
            f"claude CLI failed (rc={proc.returncode}, model={model}): {stderr_tail}"
        )

    text = (proc.stdout or "").rstrip("\n")
    return ModelResponse(
        text=text,
        prompt_tokens=max(1, len(rendered) // 4),
        completion_tokens=max(0, len(text) // 4),
        cost_usd=0.0,
        latency_s=latency_s,
        model=f"claude/{model}",
    )


# ---------------------------------------------------------------------------
# Gemini CLI provider
# ---------------------------------------------------------------------------

def _is_gemini_model(model: str) -> bool:
    if not model:
        return False
    return model == "gemini" or model.startswith("gemini/")


def _gemini_model_arg(model: str) -> str:
    """Translate `gemini/gemini-2.5-pro` into the `-m` value (`gemini-2.5-pro`).

    Bare `gemini` defaults to `gemini-2.5-pro`.
    """
    if model == "gemini":
        return "gemini-2.5-pro"
    return model.split("/", 1)[1] if "/" in model else "gemini-2.5-pro"


def _call_gemini_cli(
    prompt: str,
    system: str = "",
    *,
    model: str = "gemini-2.5-pro",
    workspace: Path | str | None = None,
    timeout: float = 600.0,
) -> ModelResponse:
    """Invoke the local `gemini` CLI in non-interactive (`-p`) mode.

    Flags: ``-p <prompt> -m <model> --output-format text --yolo --skip-trust``
    plus ``--include-directories <workspace>`` when provided. Uses the user's
    oauth-personal account (Google One AI Pro). Requires Node v22+.

    cost_usd is reported as 0 because subscription billing is opaque.
    """
    cli = shutil.which("gemini")
    if cli is None:
        raise RuntimeError(
            "gemini CLI not found on PATH. "
            "Install with `npm install -g @google/gemini-cli` and run `gemini` once to log in."
        )

    rendered = (
        f"# System\n\n{system.strip()}\n\n# Task\n\n{prompt.strip()}"
        if system.strip()
        else prompt
    )

    cmd: list[str] = [
        cli,
        "-p",
        rendered,
        "-m",
        model,
        "--output-format",
        "text",
        "--yolo",
        "--skip-trust",
    ]
    if workspace is not None:
        cmd.extend(["--include-directories", str(workspace)])

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"gemini CLI timed out after {timeout:.0f}s "
            f"(model={model}, workspace={workspace})"
        ) from e
    latency_s = time.monotonic() - started

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(
            f"gemini CLI failed (rc={proc.returncode}, model={model}): {stderr_tail}"
        )

    text = (proc.stdout or "").rstrip("\n")
    return ModelResponse(
        text=text,
        prompt_tokens=max(1, len(rendered) // 4),
        completion_tokens=max(0, len(text) // 4),
        cost_usd=0.0,
        latency_s=latency_s,
        model=f"gemini/{model}",
    )


# ---------------------------------------------------------------------------
# CLI provider dispatch helper
# ---------------------------------------------------------------------------

def _cli_provider(model: str) -> str | None:
    """Return ``"cursor"`` / ``"claude"`` / ``"gemini"`` for CLI-routed models, else None."""
    if _is_cursor_model(model):
        return "cursor"
    if _is_claude_model(model):
        return "claude"
    if _is_gemini_model(model):
        return "gemini"
    return None


# ---------------------------------------------------------------------------
# litellm provider
# ---------------------------------------------------------------------------

def _call_litellm(
    model: str,
    prompt: str,
    system: str,
    *,
    temperature: float,
    max_tokens: int | None,
    extra: dict[str, Any] | None,
) -> ModelResponse:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if extra:
        kwargs.update(extra)

    started = time.monotonic()
    try:
        response = litellm.completion(**kwargs)
    except litellm.RateLimitError:
        time.sleep(1.5)
        response = litellm.completion(**kwargs)
    latency_s = time.monotonic() - started

    prompt_tokens, completion_tokens = _extract_usage(response)
    return ModelResponse(
        text=_extract_text(response),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=_safe_cost(response),
        latency_s=latency_s,
        model=model,
    )


# ---------------------------------------------------------------------------
# unified entry point
# ---------------------------------------------------------------------------

def call_model(
    phase: Phase,
    prompt: str,
    system: str = "",
    config: Config | None = None,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    extra: dict[str, Any] | None = None,
    workspace: Path | str | None = None,
    cursor_timeout: float = 600.0,
    cli_timeout: float | None = None,
) -> ModelResponse:
    """Invoke the model assigned to `phase` and return a normalized response.

    Routes to a local CLI (cursor / claude / gemini) when the configured model
    id is prefixed with ``cursor`` / ``claude`` / ``gemini``; everything else
    goes through litellm. Retries once on litellm.RateLimitError with a short
    backoff.

    Extra keyword args:
      workspace:        Pass-through to the CLI's workspace flag (cursor:
                        ``--workspace``, claude: ``--add-dir``, gemini:
                        ``--include-directories``). Ignored by litellm.
      cursor_timeout:   Legacy alias for cli_timeout (kept for API stability,
                        used only when the phase-specific config timeout is
                        the built-in default of 600 s).
      cli_timeout:      Subprocess timeout (seconds) when routing to a CLI
                        provider. When ``None`` (the default) the value is
                        taken from ``config.runtime.cli_timeout_for(phase)``,
                        which honors per-phase overrides set via TOML or env.
    """
    cfg = config or load_config()
    model = _model_for_phase(phase, cfg)

    provider = _cli_provider(model)
    if provider is not None:
        if cli_timeout is not None:
            timeout = cli_timeout
        else:
            # Phase-aware default from config. cursor_timeout is honored as a
            # legacy override only when the user did not customize the phase
            # timeout (i.e. it still equals the built-in 600 s default).
            phase_to = cfg.runtime.cli_timeout_for(phase)
            timeout = float(phase_to) if phase_to != 600 else float(cursor_timeout)
        if provider == "cursor":
            return _call_cursor_cli(
                prompt,
                system,
                model=_cursor_model_arg(model),
                workspace=workspace,
                timeout=timeout,
            )
        if provider == "claude":
            return _call_claude_cli(
                prompt,
                system,
                model=_claude_model_arg(model),
                workspace=workspace,
                timeout=timeout,
            )
        if provider == "gemini":
            return _call_gemini_cli(
                prompt,
                system,
                model=_gemini_model_arg(model),
                workspace=workspace,
                timeout=timeout,
            )

    return _call_litellm(
        model,
        prompt,
        system,
        temperature=temperature,
        max_tokens=max_tokens,
        extra=extra,
    )
