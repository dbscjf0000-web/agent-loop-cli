"""Unit tests for ``agent_loop.mcp.handlers`` (v0.5).

All tests mock the Orchestrator so no LLM calls happen.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_loop.config import Config, load_config
from agent_loop.mcp.handlers import Handlers, RESOURCE_SPECS, TOOL_SPECS
from agent_loop.mcp.protocol import (
    ERR_METHOD_NOT_FOUND,
    ERR_NOT_FOUND,
    ERR_PRIVACY_DISABLED,
)
from agent_loop.state import TaskDir


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    """Isolated config: cross_task_memory enabled, pointing at tmp_path."""
    monkeypatch.setenv(
        "AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global")
    )
    return load_config(None)


@pytest.fixture
def handlers(config: Config, tmp_path: Path) -> Handlers:
    return Handlers(config=config, root=tmp_path / "state")


def test_initialize_returns_protocol_metadata(handlers: Handlers) -> None:
    resp = handlers.dispatch("initialize", {}, rid=1)
    assert resp.error is None
    info = resp.result
    assert info["protocolVersion"]
    assert info["serverInfo"]["name"] == "agent-loop"
    assert "tools" in info["capabilities"]
    assert "resources" in info["capabilities"]


def test_tools_list_exposes_six_tools(handlers: Handlers) -> None:
    resp = handlers.dispatch("tools/list", {}, rid=2)
    assert resp.error is None
    names = [t["name"] for t in resp.result["tools"]]
    assert names == [
        "agent_loop.run",
        "agent_loop.list",
        "agent_loop.status",
        "agent_loop.resume",
        "agent_loop.bench",
        "agent_loop.memory_show",
    ]
    # Every tool spec carries an inputSchema.
    for spec in TOOL_SPECS:
        assert "inputSchema" in spec


def test_tools_call_run_invokes_orchestrator(
    handlers: Handlers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent_loop.run` should construct an Orchestrator (mocked) and return its dict."""
    captured: dict[str, Any] = {}

    class _FakeOrch:
        def __init__(self, td: TaskDir, cfg: Config) -> None:
            captured["td"] = td
            captured["cfg"] = cfg

        def run(self, *, task: str, max_cycles: int, mode: str, max_redo: int) -> dict[str, Any]:
            captured.update(task=task, cycles=max_cycles, mode=mode, redo=max_redo)
            return {
                "task_id": td_id(captured["td"]),
                "cycles_run": 1,
                "final_status": "stop",
                "best_solution_path": None,
                "total_cost_usd": 0.0,
            }

    def td_id(td: TaskDir) -> str:
        return td.task_id

    monkeypatch.setattr("agent_loop.orchestrator.Orchestrator", _FakeOrch)

    resp = handlers.dispatch(
        "tools/call",
        {"name": "agent_loop.run", "arguments": {"task": "Implement add(a, b).", "cycles": 1}},
        rid=3,
    )
    assert resp.error is None
    body = resp.result["content"][0]["text"]
    parsed = json.loads(body)
    assert parsed["final_status"] == "stop"
    assert captured["task"] == "Implement add(a, b)."
    assert captured["cycles"] == 1


def test_tools_call_memory_show_reads_patterns(
    handlers: Handlers, tmp_path: Path
) -> None:
    gdir = tmp_path / "global"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "patterns.md").write_text(
        "CORE: alpha\nCORE: beta\nCORE: gamma\n", encoding="utf-8"
    )
    resp = handlers.dispatch(
        "tools/call",
        {"name": "agent_loop.memory_show", "arguments": {"limit": 2}},
        rid=4,
    )
    assert resp.error is None
    body = json.loads(resp.result["content"][0]["text"])
    assert body["lines"] == ["CORE: beta", "CORE: gamma"]
    assert body["total_lines"] == 3


def test_tools_call_unknown_tool_returns_error(handlers: Handlers) -> None:
    resp = handlers.dispatch(
        "tools/call",
        {"name": "agent_loop.no_such", "arguments": {}},
        rid=5,
    )
    assert resp.error is not None


def test_resources_list_returns_four(handlers: Handlers) -> None:
    resp = handlers.dispatch("resources/list", {}, rid=6)
    assert resp.error is None
    uris = [r["uri"] for r in resp.result["resources"]]
    assert len(uris) == 4
    assert any("global/patterns" in u for u in uris)
    assert any("task/{id}/solution" in u for u in uris)


def test_resources_read_solution_returns_workspace_file(
    handlers: Handlers, tmp_path: Path
) -> None:
    """A fake task with a solution.py should be readable via resources/read."""
    td = TaskDir(root=tmp_path / "state", task_id="abcd12")
    td.init()
    (td.workspace_path() / "solution.py").write_text("def f(): return 1\n", encoding="utf-8")
    resp = handlers.dispatch(
        "resources/read",
        {"uri": "agent-loop://task/abcd12/solution"},
        rid=7,
    )
    assert resp.error is None
    contents = resp.result["contents"]
    assert contents[0]["text"].startswith("def f")
    assert contents[0]["mimeType"] == "text/x-python"


def test_resources_read_global_refused_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cross_task_memory=False -> agent-loop://global/patterns must error with ERR_PRIVACY_DISABLED."""
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY", "false")
    cfg = load_config(None)
    h = Handlers(config=cfg, root=tmp_path / "state")
    resp = h.dispatch(
        "resources/read",
        {"uri": "agent-loop://global/patterns"},
        rid=8,
    )
    assert resp.error is not None
    assert resp.error["code"] == ERR_PRIVACY_DISABLED


def test_resources_read_missing_task_returns_not_found(
    handlers: Handlers,
) -> None:
    resp = handlers.dispatch(
        "resources/read",
        {"uri": "agent-loop://task/zzz999/solution"},
        rid=9,
    )
    assert resp.error is not None
    assert resp.error["code"] == ERR_NOT_FOUND


def test_unknown_method_returns_method_not_found(handlers: Handlers) -> None:
    resp = handlers.dispatch("does/not/exist", {}, rid=10)
    assert resp.error is not None
    assert resp.error["code"] == ERR_METHOD_NOT_FOUND


def test_resource_specs_match_handlers_implementation() -> None:
    """Sanity: each resource spec URI is one of the four kinds the handler implements."""
    expected = {
        "agent-loop://task/{id}/solution",
        "agent-loop://task/{id}/memory",
        "agent-loop://task/{id}/metrics",
        "agent-loop://global/patterns",
    }
    actual = {r["uri"] for r in RESOURCE_SPECS}
    assert actual == expected
