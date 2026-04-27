"""CLI tests for v0.5 ``agent-loop mcp`` subcommands."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_loop.cli import app
from agent_loop.config import load_config
from agent_loop.mcp.server import serve_stdio

runner = CliRunner()


def test_mcp_tools_lists_six(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`agent-loop mcp tools` prints all six tool names."""
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global"))
    result = runner.invoke(app, ["mcp", "tools"])
    assert result.exit_code == 0
    for name in (
        "agent_loop.run",
        "agent_loop.list",
        "agent_loop.status",
        "agent_loop.resume",
        "agent_loop.bench",
        "agent_loop.memory_show",
    ):
        assert name in result.stdout


def test_mcp_resources_lists_four(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`agent-loop mcp resources` prints the four URI patterns.

    Rich may wrap long URIs across cells, so we test on the underlying spec
    list rather than the wrapped console output.
    """
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global"))
    result = runner.invoke(app, ["mcp", "resources"])
    assert result.exit_code == 0
    # Sanity: at least the 'agent-loop://' scheme appears in output.
    assert "agent-loop://" in result.stdout
    # And the spec catalog itself must contain four URIs.
    from agent_loop.mcp.handlers import RESOURCE_SPECS
    uris = {r["uri"] for r in RESOURCE_SPECS}
    assert "agent-loop://task/{id}/solution" in uris
    assert "agent-loop://global/patterns" in uris


def test_mcp_serve_unknown_transport_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`agent-loop mcp serve --transport http` is reserved for v0.5.x and must exit 2."""
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global"))
    result = runner.invoke(app, ["mcp", "serve", "--transport", "http"])
    assert result.exit_code == 2


def test_serve_stdio_initialize_then_tools_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mock e2e: drive serve_stdio with two requests; expect two valid responses on stdout."""
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global"))
    cfg = load_config(None)

    stdin_lines = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
    )
    stdin = StringIO(stdin_lines + "\n")
    stdout = StringIO()
    rc = serve_stdio(cfg, tmp_path / "state", stdin=stdin, stdout=stdout)
    assert rc == 0

    out_lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(out_lines) == 2
    init_resp = json.loads(out_lines[0])
    list_resp = json.loads(out_lines[1])
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"]["name"] == "agent-loop"
    assert list_resp["id"] == 2
    names = [t["name"] for t in list_resp["result"]["tools"]]
    assert "agent_loop.list" in names


def test_serve_stdio_handles_parse_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed JSON line yields ERR_PARSE; server keeps going."""
    monkeypatch.setenv("AGENT_LOOP_RUNTIME_CROSS_TASK_MEMORY_DIR", str(tmp_path / "global"))
    cfg = load_config(None)

    stdin = StringIO(
        "not valid json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        + "\n"
    )
    stdout = StringIO()
    rc = serve_stdio(cfg, tmp_path / "state", stdin=stdin, stdout=stdout)
    assert rc == 0
    out_lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    # First line: parse error (id=null), second line: the tools/list reply
    parse_err = json.loads(out_lines[0])
    assert parse_err["error"]["code"] == -32700
    assert parse_err["id"] is None
    list_resp = json.loads(out_lines[1])
    assert list_resp["id"] == 5
