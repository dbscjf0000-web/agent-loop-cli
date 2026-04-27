"""Typer CLI entry point.

Commands:
  agent-loop run "<task>"           Drive a single task through R->P->I->V->J cycles.
  agent-loop list                   Show all tasks under the state root.
  agent-loop resume <task-id>       Continue a paused task from its last checkpoint.
  agent-loop config init|edit|show  Manage the user config.
  agent-loop bench [<name>]         Run a benchmark task from benchmarks/.
  agent-loop models                 Print the configured per-phase models.
  agent-loop doctor                 Sanity-check the local environment.
  agent-loop test-model <id>        Send a one-line ping to a model.
  agent-loop --version              Print version and exit.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from agent_loop import __version__
from agent_loop.config import (
    DEFAULT_USER_CONFIG,
    Config,
    JudgeSpec,
    StrategySpec,
    init_default_config,
    load_config,
    _DEFAULT_TOML,
)
from agent_loop.models import (
    ModelResponse,
    _call_claude_cli,
    _call_cursor_cli,
    _call_gemini_cli,
    _call_litellm,
    _claude_model_arg,
    _cli_provider,
    _cursor_model_arg,
    _gemini_model_arg,
    _is_claude_model,
    _is_cursor_model,
    _is_gemini_model,
)
from agent_loop.orchestrator import Orchestrator
from agent_loop.state import TaskDir, list_tasks, new_task_id

app = typer.Typer(
    name="agent-loop",
    help="R->P->I->V->J agent loop CLI (multi-model via litellm).",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(name="config", help="Manage agent-loop config.", no_args_is_help=True)
app.add_typer(config_app, name="config")
memory_app = typer.Typer(
    name="memory",
    help="(v0.4) Manage cross-task global memory (~/.agent-loop/global/).",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")

console = Console()


# ---------------------------------------------------------------------------
# root: --version
# ---------------------------------------------------------------------------
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _override_judges(cfg: Config, judges: list[str] | None) -> Config:
    """Replace cfg.runtime.judges with explicit `--judge` flags (each weight=1.0).

    Empty list means user did not pass any flags -> leave cfg unchanged.
    """
    if not judges:
        return cfg
    cfg.runtime.judges = [JudgeSpec(provider=p, weight=1.0) for p in judges]
    return cfg


def _override_strategies(cfg: Config, strategies: list[str] | None) -> Config:
    """Replace cfg.runtime.strategies with explicit `--strategy` flags (each weight=1.0).

    Empty list means user did not pass any flags -> leave cfg unchanged.
    """
    if not strategies:
        return cfg
    cfg.runtime.strategies = [StrategySpec(provider=p, weight=1.0) for p in strategies]
    return cfg


def _override_runtime_v031(
    cfg: Config,
    *,
    cli_timeout: int | None,
    cli_timeout_verify: int | None,
    cli_timeout_judge: int | None,
    judge_always_llm: bool,
) -> Config:
    """v0.3.1 — apply --cli-timeout / --cli-timeout-{verify,judge} / --judge-always-llm.

    Only `verify` and `judge` get dedicated flags because those are the two
    phases the live verification surfaced as needing tuning (claude verify
    600 s timeout, judge first-cycle skip). Other phases remain settable via
    config TOML or env var.
    """
    if cli_timeout is not None:
        cfg.runtime.cli_timeout = int(cli_timeout)
    if cli_timeout_verify is not None:
        cfg.runtime.cli_timeout_verify = int(cli_timeout_verify)
    if cli_timeout_judge is not None:
        cfg.runtime.cli_timeout_judge = int(cli_timeout_judge)
    if judge_always_llm:
        cfg.runtime.judge_always_llm = True
    return cfg


def _override_runtime_v04(cfg: Config, *, no_cross_task: bool) -> Config:
    """v0.4 — apply --no-cross-task (one-shot opt-out, leaves config TOML alone)."""
    if no_cross_task:
        cfg.runtime.cross_task_memory = False
    return cfg


@app.command("run")
def cmd_run(
    task: str = typer.Argument(..., help="Task description (free-form prose)."),
    cycles: int = typer.Option(5, "--cycles", help="Maximum number of R->P->I->V->J cycles."),
    mode: str = typer.Option("auto", "--mode", help="auto | supervised"),
    max_redo: int = typer.Option(3, "--max-redo", help="Max consecutive non-improving cycles."),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to a config TOML."),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Reuse this id (else random)."),
    root: Path = typer.Option(Path("./.agent_loop"), "--root", help="State root directory."),
    judge: list[str] = typer.Option(
        [],
        "--judge",
        help="(v0.3) Multi-judge provider id, repeatable. Cross-vendor recommended. "
        "Example: --judge claude/default --judge gemini/gemini-2.5-flash",
    ),
    strategy: list[str] = typer.Option(
        [],
        "--strategy",
        help="(v0.3) Multi-strategy plan provider id, repeatable. Cross-vendor recommended. "
        "Example: --strategy claude/default --strategy cursor/auto",
    ),
    cli_timeout: Optional[int] = typer.Option(
        None,
        "--cli-timeout",
        help="(v0.3.1) Default subprocess timeout (seconds) for CLI providers. Built-in default: 600.",
    ),
    cli_timeout_verify: Optional[int] = typer.Option(
        None,
        "--cli-timeout-verify",
        help="(v0.3.1) Per-phase override for verify (overrides --cli-timeout for verify only).",
    ),
    cli_timeout_judge: Optional[int] = typer.Option(
        None,
        "--cli-timeout-judge",
        help="(v0.3.1) Per-phase override for judge (overrides --cli-timeout for judge only).",
    ),
    judge_always_llm: bool = typer.Option(
        False,
        "--judge-always-llm",
        help="(v0.3.1) Disable the first-cycle short-circuit and always invoke the judge LLM. "
        "Required for genuine multi-judge cross-vendor verification when score>=0.95 on cycle 1.",
    ),
    no_cross_task: bool = typer.Option(
        False,
        "--no-cross-task",
        help="(v0.4) Disable cross-task global memory for this run only (does not modify config).",
    ),
) -> None:
    """Run a fresh task through the loop."""
    if mode not in ("auto", "supervised"):
        raise typer.BadParameter("mode must be 'auto' or 'supervised'")

    cfg = load_config(config_path)
    cfg = _override_judges(cfg, judge)
    cfg = _override_strategies(cfg, strategy)
    cfg = _override_runtime_v031(
        cfg,
        cli_timeout=cli_timeout,
        cli_timeout_verify=cli_timeout_verify,
        cli_timeout_judge=cli_timeout_judge,
        judge_always_llm=judge_always_llm,
    )
    cfg = _override_runtime_v04(cfg, no_cross_task=no_cross_task)
    tid = task_id or new_task_id()
    td = TaskDir(root=root, task_id=tid)
    td.init()

    console.print(f"[bold green][OK][/bold green] task_id = [magenta]{tid}[/magenta]")
    console.print(f"     root    = {td.path}")
    console.print(f"     cycles  = {cycles}, mode = {mode}, max_redo = {max_redo}")
    if cfg.runtime.judges:
        provs = ", ".join(j.provider for j in cfg.runtime.judges)
        console.print(f"     judges  = [yellow]{provs}[/yellow] ({len(cfg.runtime.judges)})")
    if cfg.runtime.strategies:
        sprovs = ", ".join(s.provider for s in cfg.runtime.strategies)
        console.print(
            f"     strats  = [yellow]{sprovs}[/yellow] ({len(cfg.runtime.strategies)})"
        )

    orch = Orchestrator(td, cfg, console=console)
    result = orch.run(task=task, max_cycles=cycles, mode=mode, max_redo=max_redo)
    _print_result(result)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@app.command("list")
def cmd_list(
    root: Path = typer.Option(Path("./.agent_loop"), "--root", help="State root directory."),
) -> None:
    """List all task directories under --root."""
    tasks = list_tasks(root)
    if not tasks:
        console.print(f"[yellow]No tasks under {root}[/yellow]")
        return

    table = Table(title=f"agent-loop tasks @ {root}")
    table.add_column("task_id", style="magenta")
    table.add_column("path", style="cyan")
    table.add_column("modified", style="green")
    for t in tasks:
        ts = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(t.task_id, str(t.path), ts)
    console.print(table)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------
@app.command("resume")
def cmd_resume(
    task_id: str = typer.Argument(...),
    root: Path = typer.Option(Path("./.agent_loop"), "--root"),
    cycles: int = typer.Option(5, "--cycles"),
    mode: str = typer.Option("auto", "--mode"),
    max_redo: int = typer.Option(3, "--max-redo"),
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Continue a task from its last checkpoint."""
    td = TaskDir(root=root, task_id=task_id)
    if not td.path.exists():
        console.print(f"[red][X][/red] no task at {td.path}")
        raise typer.Exit(1)

    task_text = td.task_md_path().read_text(encoding="utf-8") if td.task_md_path().exists() else ""
    if not task_text.strip():
        console.print(f"[red][X][/red] task.md is empty for {task_id}")
        raise typer.Exit(1)

    cfg = load_config(config_path)
    console.print(f"[bold green][OK][/bold green] resuming [magenta]{task_id}[/magenta]")
    orch = Orchestrator(td, cfg, console=console)
    result = orch.run(task=task_text, max_cycles=cycles, mode=mode, max_redo=max_redo)
    _print_result(result)


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------
@app.command("bench")
def cmd_bench(
    name: Optional[str] = typer.Argument(None, help="benchmark name (without .yaml)"),
    quick: bool = typer.Option(False, "--quick", help="Run only binary_search."),
    cycles: Optional[int] = typer.Option(None, "--cycles", help="Override max cycles from yaml."),
    max_redo: Optional[int] = typer.Option(None, "--max-redo", help="Override max_redo from yaml."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse yaml + write task.md, but skip every LLM call. Smoke test only.",
    ),
    root: Path = typer.Option(Path("./.agent_loop"), "--root"),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    judge: list[str] = typer.Option(
        [],
        "--judge",
        help="(v0.3) Multi-judge provider id, repeatable. Same semantics as `agent-loop run --judge`.",
    ),
    strategy: list[str] = typer.Option(
        [],
        "--strategy",
        help="(v0.3) Multi-strategy provider id, repeatable. Same semantics as `agent-loop run --strategy`.",
    ),
    cli_timeout: Optional[int] = typer.Option(
        None, "--cli-timeout", help="(v0.3.1) Default CLI subprocess timeout (seconds)."
    ),
    cli_timeout_verify: Optional[int] = typer.Option(
        None, "--cli-timeout-verify", help="(v0.3.1) Per-phase verify override."
    ),
    cli_timeout_judge: Optional[int] = typer.Option(
        None, "--cli-timeout-judge", help="(v0.3.1) Per-phase judge override."
    ),
    judge_always_llm: bool = typer.Option(
        False,
        "--judge-always-llm",
        help="(v0.3.1) Disable judge first-cycle short-circuit (always call LLM).",
    ),
    no_cross_task: bool = typer.Option(
        False,
        "--no-cross-task",
        help="(v0.4) Disable cross-task global memory for this run only.",
    ),
) -> None:
    """Run a benchmark task from benchmarks/."""
    bench_dir = _find_benchmarks_dir()
    if bench_dir is None:
        console.print("[red][X][/red] benchmarks/ directory not found")
        raise typer.Exit(1)

    if quick:
        names = ["binary_search"]
    elif name:
        names = [name]
    else:
        names = sorted(p.stem for p in bench_dir.glob("*.yaml") if p.stem != "README")

    cfg = load_config(config_path)
    cfg = _override_judges(cfg, judge)
    cfg = _override_strategies(cfg, strategy)
    cfg = _override_runtime_v031(
        cfg,
        cli_timeout=cli_timeout,
        cli_timeout_verify=cli_timeout_verify,
        cli_timeout_judge=cli_timeout_judge,
        judge_always_llm=judge_always_llm,
    )
    cfg = _override_runtime_v04(cfg, no_cross_task=no_cross_task)
    for n in names:
        path = bench_dir / f"{n}.yaml"
        if not path.exists():
            console.print(f"[red][X][/red] benchmark not found: {path}")
            continue
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        task_text = _bench_to_task_md(spec)
        budget = spec.get("budget") or {}
        bench_cycles = int(cycles if cycles is not None else budget.get("max_cycles", 5))
        bench_redo = int(max_redo if max_redo is not None else budget.get("max_redo", 3))

        tid = new_task_id()
        td = TaskDir(root=root, task_id=f"bench-{n}-{tid}")
        td.init()
        td.task_md_path().write_text(task_text, encoding="utf-8")
        # v0.2: persist a rubric.json next to the task so VerifyEngine drives the V phase.
        crit = spec.get("success_criteria") or []
        if crit:
            from agent_loop.verify_engine import yaml_to_rubric

            rubric = yaml_to_rubric(crit)
            td.write_artifact("rubric.json", rubric)
        console.print(f"[bold cyan][run][/bold cyan] benchmark={n} task_id={td.task_id}")

        if dry_run:
            console.print(
                f"  [yellow][dry-run][/yellow] task.md written ({len(task_text)} chars)"
                f", cycles={bench_cycles}, max_redo={bench_redo}"
            )
            console.print(f"  [yellow][dry-run][/yellow] no LLM calls performed")
            console.print(f"  [yellow][dry-run][/yellow] task dir: {td.path}")
            continue

        orch = Orchestrator(td, cfg, console=console)
        result = orch.run(
            task=task_text, max_cycles=bench_cycles, mode="auto", max_redo=bench_redo
        )
        _print_result(result)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
_KNOWN_CURSOR_MODELS = ("auto", "sonnet-4", "sonnet-4-thinking", "gpt-5")
_KNOWN_CLAUDE_MODELS = ("default",)
_KNOWN_GEMINI_MODELS = ("gemini-2.5-pro", "gemini-2.5-flash")


def _list_cursor_models() -> list[str]:
    """Best-effort: ask cursor-agent for its model list. Fall back to known set."""
    cli = shutil.which("cursor-agent")
    if cli is None:
        return list(_KNOWN_CURSOR_MODELS)
    try:
        proc = subprocess.run(
            [cli, "--list-models"], capture_output=True, text=True, timeout=10, check=False
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return [
                line.strip().lstrip("-* ").split()[0]
                for line in proc.stdout.splitlines()
                if line.strip()
            ]
    except Exception:
        pass
    return list(_KNOWN_CURSOR_MODELS)


def _list_claude_models() -> list[str]:
    """Static list. Claude Code CLI does not expose --list-models; the user
    selects model via settings/--model, but our adapter currently uses default."""
    if shutil.which("claude") is None:
        return []
    return list(_KNOWN_CLAUDE_MODELS)


def _list_gemini_models() -> list[str]:
    """Static list. Gemini CLI does not expose a list endpoint."""
    if shutil.which("gemini") is None:
        return []
    return list(_KNOWN_GEMINI_MODELS)


@app.command("models")
def cmd_models(
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Show the per-phase model assignments and available cursor-agent models."""
    cfg = load_config(config_path)
    table = Table(title="Per-phase models")
    table.add_column("phase", style="cyan")
    table.add_column("model", style="magenta")
    for phase in ("research", "plan", "implement", "verify", "judge"):
        table.add_row(phase, getattr(cfg.models, phase))
    console.print(table)

    cursor_models = _list_cursor_models()
    if cursor_models:
        ct = Table(title="cursor-agent models (use as cursor/<model>)")
        ct.add_column("model", style="green")
        for m in cursor_models:
            ct.add_row(m)
        console.print(ct)

    claude_models = _list_claude_models()
    if claude_models:
        clt = Table(title="claude (Claude Code) models (use as claude/<model>)")
        clt.add_column("model", style="green")
        for m in claude_models:
            clt.add_row(m)
        console.print(clt)

    gemini_models = _list_gemini_models()
    if gemini_models:
        gt = Table(title="gemini models (use as gemini/<model>)")
        gt.add_column("model", style="green")
        for m in gemini_models:
            gt.add_row(m)
        console.print(gt)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def _mask(value: str | None) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return value[0] + "***"
    return f"{value[:4]}...{value[-4:]}"


@app.command("doctor")
def cmd_doctor(
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Sanity-check the local environment for agent-loop-cli."""
    table = Table(title="agent-loop doctor")
    table.add_column("check", style="cyan")
    table.add_column("status", style="bold")
    table.add_column("detail", style="white")

    # Python
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    table.add_row(
        "python",
        "[green]OK[/green]" if py_ok else "[red]FAIL[/red]",
        f"{py} on {platform.system()} {platform.release()}",
    )

    # config
    try:
        cfg = load_config(config_path)
        from agent_loop.config import _resolve_config_path

        resolved = _resolve_config_path(config_path)
        src = str(resolved) if resolved else "(built-in defaults)"
        table.add_row("config", "[green]OK[/green]", src)
    except Exception as e:
        cfg = None
        table.add_row("config", "[red]FAIL[/red]", str(e)[:200])

    # env vars
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CURSOR_API_KEY"):
        val = os.environ.get(name)
        status = "[green]set[/green]" if val else "[yellow]unset[/yellow]"
        table.add_row(f"env: {name}", status, _mask(val))

    # cursor-agent
    cursor_path = shutil.which("cursor-agent")
    if cursor_path:
        table.add_row("cursor-agent: PATH", "[green]OK[/green]", cursor_path)
        try:
            s = subprocess.run(
                [cursor_path, "status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if s.returncode == 0:
                table.add_row(
                    "cursor-agent: status",
                    "[green]OK[/green]",
                    (s.stdout or s.stderr).strip().splitlines()[0][:200] if (s.stdout or s.stderr).strip() else "logged in",
                )
            else:
                table.add_row(
                    "cursor-agent: status",
                    "[red]FAIL[/red]",
                    f"rc={s.returncode}: {(s.stderr or s.stdout).strip()[:200]}",
                )
        except Exception as e:
            table.add_row("cursor-agent: status", "[red]FAIL[/red]", str(e)[:200])
    else:
        table.add_row(
            "cursor-agent: PATH",
            "[yellow]missing[/yellow]",
            "install cursor-agent + run `cursor-agent login`",
        )

    # claude (Claude Code CLI)
    claude_path = shutil.which("claude")
    if claude_path:
        table.add_row("claude: PATH", "[green]OK[/green]", claude_path)
        try:
            s = subprocess.run(
                [claude_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if s.returncode == 0:
                first = (s.stdout or s.stderr).strip().splitlines()
                table.add_row(
                    "claude: version",
                    "[green]OK[/green]",
                    first[0][:200] if first else "ok",
                )
            else:
                table.add_row(
                    "claude: version",
                    "[red]FAIL[/red]",
                    f"rc={s.returncode}: {(s.stderr or s.stdout).strip()[:200]}",
                )
        except Exception as e:
            table.add_row("claude: version", "[red]FAIL[/red]", str(e)[:200])
    else:
        table.add_row(
            "claude: PATH",
            "[yellow]missing[/yellow]",
            "install Claude Code CLI + run `claude` once to log in",
        )

    # gemini
    gemini_path = shutil.which("gemini")
    if gemini_path:
        table.add_row("gemini: PATH", "[green]OK[/green]", gemini_path)
        try:
            s = subprocess.run(
                [gemini_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if s.returncode == 0:
                first = (s.stdout or s.stderr).strip().splitlines()
                table.add_row(
                    "gemini: version",
                    "[green]OK[/green]",
                    first[0][:200] if first else "ok",
                )
            else:
                table.add_row(
                    "gemini: version",
                    "[red]FAIL[/red]",
                    f"rc={s.returncode}: {(s.stderr or s.stdout).strip()[:200]}",
                )
        except Exception as e:
            table.add_row("gemini: version", "[red]FAIL[/red]", str(e)[:200])
    else:
        table.add_row(
            "gemini: PATH",
            "[yellow]missing[/yellow]",
            "npm install -g @google/gemini-cli + run `gemini` once to log in",
        )

    # node (gemini requires v22+)
    node_path = shutil.which("node")
    if node_path:
        try:
            s = subprocess.run(
                [node_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            ver = (s.stdout or s.stderr).strip().splitlines()[0] if s.stdout or s.stderr else ""
            # parse major: "v22.22.2" -> 22
            major = 0
            try:
                major = int(ver.lstrip("v").split(".", 1)[0])
            except Exception:
                pass
            ok = major >= 22
            table.add_row(
                "node",
                "[green]OK[/green]" if ok else "[yellow]old[/yellow]",
                f"{ver}{' (gemini requires >=22)' if not ok else ''}",
            )
        except Exception as e:
            table.add_row("node", "[red]FAIL[/red]", str(e)[:200])
    else:
        table.add_row("node", "[yellow]missing[/yellow]", "gemini CLI requires Node v22+")

    # litellm version
    try:
        import litellm

        ver = getattr(litellm, "__version__", "unknown")
        table.add_row("litellm", "[green]OK[/green]", str(ver))
    except Exception as e:
        table.add_row("litellm", "[red]FAIL[/red]", str(e)[:200])

    console.print(table)
    if cfg is not None:
        # Show which providers each phase needs
        used = sorted({getattr(cfg.models, p) for p in ("research", "plan", "implement", "verify", "judge")})
        console.print(f"  configured models in use: [magenta]{', '.join(used)}[/magenta]")


# ---------------------------------------------------------------------------
# test-model
# ---------------------------------------------------------------------------
@app.command("test-model")
def cmd_test_model(
    model_id: str = typer.Argument(..., help="Model id (e.g. 'cursor/auto', 'claude/default', 'gemini/gemini-2.5-pro', 'anthropic/claude-haiku-4-5')."),
    timeout: int = typer.Option(120, "--timeout", help="CLI provider timeout (seconds)."),
) -> None:
    """Send a short 'Reply with OK' ping to the given model and print the result."""
    prompt = "Reply with the single word: OK"
    console.print(f"[cyan]>[/cyan] pinging [magenta]{model_id}[/magenta] ...")
    try:
        provider = _cli_provider(model_id)
        if provider == "cursor":
            resp = _call_cursor_cli(
                prompt,
                system="",
                model=_cursor_model_arg(model_id),
                workspace=None,
                timeout=float(timeout),
            )
        elif provider == "claude":
            resp = _call_claude_cli(
                prompt,
                system="",
                model=_claude_model_arg(model_id),
                workspace=None,
                timeout=float(timeout),
            )
        elif provider == "gemini":
            resp = _call_gemini_cli(
                prompt,
                system="",
                model=_gemini_model_arg(model_id),
                workspace=None,
                timeout=float(timeout),
            )
        else:
            resp = _call_litellm(
                model_id,
                prompt,
                system="",
                temperature=0.0,
                max_tokens=8,
                extra=None,
            )
    except Exception as e:
        console.print(f"[red][X] {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"test-model: {model_id}")
    table.add_column("key", style="cyan")
    table.add_column("value", style="magenta")
    table.add_row("response", repr(resp.text[:200]))
    table.add_row("latency_s", f"{resp.latency_s:.2f}")
    table.add_row("cost_usd", f"{resp.cost_usd:.6f}")
    table.add_row("prompt_tokens", str(resp.prompt_tokens))
    table.add_row("completion_tokens", str(resp.completion_tokens))
    table.add_row("model", resp.model)
    console.print(table)


# ---------------------------------------------------------------------------
# config init / show / edit
# ---------------------------------------------------------------------------
@config_app.command("init")
def cmd_config_init(
    print_only: bool = typer.Option(False, "--print", help="Write to stdout instead of disk."),
    path: Optional[Path] = typer.Option(None, "--path"),
) -> None:
    """Write a default config (or print it)."""
    if print_only:
        typer.echo(_DEFAULT_TOML)
        return
    target = path or DEFAULT_USER_CONFIG
    try:
        written = init_default_config(target)
    except FileExistsError as e:
        console.print(f"[yellow][!][/yellow] {e}")
        raise typer.Exit(1)
    console.print(f"[bold green][OK][/bold green] wrote {written}")


@config_app.command("show")
def cmd_config_show(
    path: Optional[Path] = typer.Option(None, "--path"),
) -> None:
    """Print the merged config (file + env)."""
    cfg = load_config(path)
    console.print_json(json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False))


@config_app.command("edit")
def cmd_config_edit(
    path: Optional[Path] = typer.Option(None, "--path"),
) -> None:
    """Open the user config in $EDITOR."""
    target = path or DEFAULT_USER_CONFIG
    if not target.exists():
        console.print(f"[yellow][!][/yellow] no config at {target}; running 'config init' first")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_DEFAULT_TOML, encoding="utf-8")
    editor = os.environ.get("EDITOR") or shutil.which("vim") or shutil.which("vi") or "vi"
    subprocess.call([editor, str(target)])


# ---------------------------------------------------------------------------
# memory (v0.4)
# ---------------------------------------------------------------------------
def _global_dir(config_path: Optional[Path] = None) -> Path:
    """Resolve the cross-task memory directory from config (env-overridable)."""
    cfg = load_config(config_path)
    return Path(cfg.runtime.cross_task_memory_dir).expanduser()


@memory_app.command("path")
def cmd_memory_path(
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Print the cross-task memory directory path."""
    d = _global_dir(config_path)
    typer.echo(str(d))


@memory_app.command("show")
def cmd_memory_show(
    limit: int = typer.Option(50, "--limit", help="Show the last N lines (default 50)."),
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Print the last N lines of patterns.md."""
    d = _global_dir(config_path)
    p = d / "patterns.md"
    if not p.exists():
        console.print(f"[yellow]no patterns.md at {p}[/yellow]")
        return
    text = p.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        console.print("[yellow](patterns.md is empty)[/yellow]")
        return
    tail = lines[-int(limit):] if limit > 0 else lines
    for ln in tail:
        console.print(ln)


@memory_app.command("list")
def cmd_memory_list(
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Show task_index.jsonl as a table."""
    d = _global_dir(config_path)
    p = d / "task_index.jsonl"
    if not p.exists():
        console.print(f"[yellow]no task_index.jsonl at {p}[/yellow]")
        return
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        console.print("[yellow](task_index.jsonl is empty)[/yellow]")
        return
    table = Table(title=f"agent-loop tasks @ {p}")
    table.add_column("task_id", style="magenta")
    table.add_column("score", style="green")
    table.add_column("cycles", style="cyan")
    table.add_column("status", style="yellow")
    table.add_column("first_line", style="white")
    for r in rows:
        ws = r.get("weighted_score")
        score_s = f"{ws:.3f}" if isinstance(ws, (int, float)) else "-"
        table.add_row(
            str(r.get("task_id", "?"))[:12],
            score_s,
            str(r.get("cycles", "-")),
            str(r.get("final_status", "-"))[:14],
            (r.get("task_md_first_line") or "")[:60],
        )
    console.print(table)


@memory_app.command("wipe")
def cmd_memory_wipe(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    config_path: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Delete the cross-task memory directory after confirmation."""
    d = _global_dir(config_path)
    if not d.exists():
        console.print(f"[yellow]nothing to wipe — {d} does not exist[/yellow]")
        return
    if not yes:
        if not typer.confirm(f"Permanently delete {d}? This cannot be undone."):
            console.print("[yellow]aborted[/yellow]")
            return
    shutil.rmtree(d)
    console.print(f"[bold green][OK][/bold green] wiped {d}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _print_result(result: dict) -> None:
    table = Table(title="Run summary")
    table.add_column("key", style="cyan")
    table.add_column("value", style="magenta")
    for k, v in result.items():
        table.add_row(k, str(v))
    console.print(table)


def _find_benchmarks_dir() -> Path | None:
    """benchmarks/ may live next to cwd, the package, or the repo root."""
    for cand in (
        Path.cwd() / "benchmarks",
        Path(__file__).resolve().parents[2] / "benchmarks",
        Path(__file__).resolve().parent / "benchmarks",
    ):
        if cand.is_dir():
            return cand
    return None


def _bench_to_task_md(spec: dict) -> str:
    parts = [f"# {spec.get('name', 'benchmark')}", ""]
    if spec.get("description"):
        parts += ["## Description", spec["description"].rstrip(), ""]
    parts += ["## Task", spec.get("task", "").rstrip(), ""]
    crit = spec.get("success_criteria") or []
    if crit:
        parts.append("## Success Criteria")
        for c in crit:
            parts.append(f"- axis: {c.get('axis')} (weight {c.get('weight', '?')})")
            for key in ("test", "target", "measure", "threshold"):
                if c.get(key):
                    parts.append(f"  - {key}: {c[key]}")
        parts.append("")
    return "\n".join(parts)


if __name__ == "__main__":
    app()
