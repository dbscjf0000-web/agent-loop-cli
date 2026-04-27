from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agent_loop import __version__
from agent_loop.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_init_print() -> None:
    result = runner.invoke(app, ["config", "init", "--print"])
    assert result.exit_code == 0
    assert "[models]" in result.stdout
    assert "[budget]" in result.stdout


def test_models_table() -> None:
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    out = result.stdout
    assert "research" in out and "implement" in out and "judge" in out


def test_list_empty(tmp_path: Path) -> None:
    target = tmp_path / "no-such-root"
    result = runner.invoke(app, ["list", "--root", str(target)])
    assert result.exit_code == 0
    assert "No tasks" in result.stdout


def test_help_includes_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "list", "resume", "config", "bench", "models"):
        assert cmd in result.stdout
