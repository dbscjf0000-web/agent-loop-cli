from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import models as models_mod
from agent_loop.config import Config
from agent_loop.models import ModelResponse, call_model


def _fake_completion_response(text: str = "ok", pt: int = 10, ct: int = 5) -> SimpleNamespace:
    """Mimic the bits of a litellm/OpenAI ChatCompletion that call_model touches."""
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_call_model_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _fake_completion_response("hello", pt=12, ct=7)

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0042)

    cfg = Config()
    resp = call_model("research", "say hi", system="be brief", config=cfg)

    assert isinstance(resp, ModelResponse)
    assert resp.text == "hello"
    assert resp.prompt_tokens == 12
    assert resp.completion_tokens == 7
    assert resp.cost_usd == pytest.approx(0.0042)
    assert resp.latency_s >= 0.0
    assert resp.model == cfg.models.research

    # System + user messages were forwarded in the right order.
    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[1] == {"role": "user", "content": "say hi"}
    assert captured["temperature"] == 0.0
    assert captured["model"] == cfg.models.research


def test_call_model_phase_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_models: list[str] = []

    def fake_completion(**kwargs: Any) -> SimpleNamespace:
        seen_models.append(kwargs["model"])
        return _fake_completion_response()

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)

    cfg = Config()
    for phase in ("research", "plan", "implement", "verify", "judge"):
        call_model(phase, "ping", config=cfg)  # type: ignore[arg-type]

    assert seen_models == [
        cfg.models.research,
        cfg.models.plan,
        cfg.models.implement,
        cfg.models.verify,
        cfg.models.judge,
    ]


def test_call_model_retries_once_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_completion(**kwargs: Any) -> SimpleNamespace:
        calls["n"] += 1
        if calls["n"] == 1:
            raise models_mod.litellm.RateLimitError(
                "slow down", model=kwargs["model"], llm_provider="test"
            )
        return _fake_completion_response("recovered", pt=1, ct=2)

    monkeypatch.setattr(models_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(models_mod.litellm, "completion_cost", lambda **_: 0.0)
    # Skip the real backoff sleep.
    monkeypatch.setattr(models_mod.time, "sleep", lambda _s: None)

    resp = call_model("verify", "ping", config=Config())
    assert resp.text == "recovered"
    assert calls["n"] == 2
