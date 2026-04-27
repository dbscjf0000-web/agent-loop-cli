"""Thin litellm wrapper that maps a phase name to a configured model.

Two providers are supported:
  - litellm (default): any `provider/model` recognized by litellm.
  - cursor-agent CLI: model strings of the form `cursor` or `cursor/<model>`,
    invoked via the locally installed `cursor-agent` binary in `--print` mode.
    The CLI is itself an agent — a single call may use tools, edit files, and
    take tens of seconds to several minutes.

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
from dataclasses import dataclass
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
) -> ModelResponse:
    """Invoke the model assigned to `phase` and return a normalized response.

    Routes to the cursor-agent CLI when the configured model starts with
    ``cursor`` / ``cursor/...``; everything else goes through litellm.
    Retries once on litellm.RateLimitError with a short backoff.

    Extra keyword args:
      workspace:        Pass-through to cursor-agent ``--workspace``. Ignored
                        by litellm.
      cursor_timeout:   Hard subprocess timeout when using cursor-agent.
    """
    cfg = config or load_config()
    model = _model_for_phase(phase, cfg)

    if _is_cursor_model(model):
        return _call_cursor_cli(
            prompt,
            system,
            model=_cursor_model_arg(model),
            workspace=workspace,
            timeout=cursor_timeout,
        )

    return _call_litellm(
        model,
        prompt,
        system,
        temperature=temperature,
        max_tokens=max_tokens,
        extra=extra,
    )
