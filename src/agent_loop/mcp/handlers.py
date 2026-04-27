"""MCP handlers — dispatch ``initialize`` / ``tools`` / ``resources`` calls.

Each tool call constructs fresh ContextEngine / TaskDir / Orchestrator
objects (stateless leaf, like the rest of the codebase). All handlers
trap exceptions and return a JSON-RPC ``error`` instead of crashing the
server loop.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_loop import __version__ as _agent_loop_version
from agent_loop.config import Config
from agent_loop.mcp import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION
from agent_loop.mcp.protocol import (
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    ERR_NOT_FOUND,
    ERR_PRIVACY_DISABLED,
    ERR_TOOL_FAILED,
    Response,
    make_error,
)
from agent_loop.state import TaskDir, list_tasks, new_task_id


# ---------------------------------------------------------------------------
# tool / resource catalogs (single source of truth)
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "agent_loop.run",
        "description": "Drive a fresh task through R->P->I->V->J cycles (sync, returns RunResult).",
        "inputSchema": {
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "description": "Task description (free-form prose)."},
                "cycles": {"type": "integer", "minimum": 1, "default": 5},
                "mode": {"type": "string", "enum": ["auto", "supervised"], "default": "auto"},
                "max_redo": {"type": "integer", "minimum": 1, "default": 3},
                "cross_task": {"type": "boolean", "default": True,
                               "description": "Override runtime.cross_task_memory for this call."},
            },
        },
    },
    {
        "name": "agent_loop.list",
        "description": "List task directories under the state root.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Override the state root (default: server's root)."},
            },
        },
    },
    {
        "name": "agent_loop.status",
        "description": "Return the latest cycle / phase / score for a task.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "root": {"type": "string"},
            },
        },
    },
    {
        "name": "agent_loop.resume",
        "description": "Continue a task from its last checkpoint (sync).",
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "cycles": {"type": "integer", "minimum": 1, "default": 5},
                "max_redo": {"type": "integer", "minimum": 1, "default": 3},
            },
        },
    },
    {
        "name": "agent_loop.bench",
        "description": "Run a benchmark task from benchmarks/ by name.",
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Benchmark stem (without .yaml)."},
                "cycles": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "agent_loop.memory_show",
        "description": "Read the trailing N lines of cross-task patterns.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "default": 50},
            },
        },
    },
]


RESOURCE_SPECS: list[dict[str, Any]] = [
    {
        "uri": "agent-loop://task/{id}/solution",
        "name": "Solution code",
        "description": "workspace/best_solution.py (or solution.py) for the task.",
        "mimeType": "text/x-python",
    },
    {
        "uri": "agent-loop://task/{id}/memory",
        "name": "Task memory",
        "description": "memory/episodic.md + memory/core_facts.md, concatenated.",
        "mimeType": "text/markdown",
    },
    {
        "uri": "agent-loop://task/{id}/metrics",
        "name": "Task metrics",
        "description": "telemetry/metrics.jsonl (per-phase rows).",
        "mimeType": "application/x-jsonlines",
    },
    {
        "uri": "agent-loop://global/patterns",
        "name": "Global cross-task patterns",
        "description": "~/.agent-loop/global/patterns.md. Refused when cross_task_memory=False.",
        "mimeType": "text/markdown",
    },
]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

class Handlers:
    """Stateless dispatcher. Construct once per server, reuse for every call."""

    def __init__(self, config: Config, root: Path) -> None:
        self.config = config
        self.root = Path(root)

    # ------------------------------------------------------------------
    # method dispatch (called by server.py's main loop)
    # ------------------------------------------------------------------
    def dispatch(self, method: str, params: dict[str, Any] | None, rid: Any) -> Response:
        params = params or {}
        try:
            if method == "initialize":
                return Response(id=rid, result=self._handle_initialize(params))
            if method == "tools/list":
                return Response(id=rid, result=self._handle_tools_list(params))
            if method == "tools/call":
                return Response(id=rid, result=self._handle_tools_call(params))
            if method == "resources/list":
                return Response(id=rid, result=self._handle_resources_list(params))
            if method == "resources/read":
                return Response(id=rid, result=self._handle_resources_read(params))
            if method in ("notifications/initialized", "ping"):
                # MCP heartbeat / notifications. Reply with empty success.
                return Response(id=rid, result={})
            return make_error(rid, ERR_METHOD_NOT_FOUND, f"method not found: {method}")
        except _PrivacyError as e:
            return make_error(rid, ERR_PRIVACY_DISABLED, str(e))
        except _NotFoundError as e:
            return make_error(rid, ERR_NOT_FOUND, str(e))
        except _BadParamsError as e:
            return make_error(rid, ERR_INVALID_PARAMS, str(e))
        except Exception as e:  # never let a bad call kill the server
            return make_error(rid, ERR_TOOL_FAILED, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # initialize
    # ------------------------------------------------------------------
    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                           "agentLoopVersion": _agent_loop_version},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
        }

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": TOOL_SPECS}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            raise _BadParamsError("'name' must be a string")
        if not isinstance(args, dict):
            raise _BadParamsError("'arguments' must be an object")

        if name == "agent_loop.run":
            return _wrap_text(self._tool_run(args))
        if name == "agent_loop.list":
            return _wrap_text(self._tool_list(args))
        if name == "agent_loop.status":
            return _wrap_text(self._tool_status(args))
        if name == "agent_loop.resume":
            return _wrap_text(self._tool_resume(args))
        if name == "agent_loop.bench":
            return _wrap_text(self._tool_bench(args))
        if name == "agent_loop.memory_show":
            return _wrap_text(self._tool_memory_show(args))
        raise _BadParamsError(f"unknown tool: {name}")

    # ------------------------------------------------------------------
    # individual tool implementations
    # ------------------------------------------------------------------
    def _tool_run(self, args: dict[str, Any]) -> str:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise _BadParamsError("'task' is required and must be non-empty")
        cycles = int(args.get("cycles", 5))
        mode = args.get("mode", "auto")
        if mode not in ("auto", "supervised"):
            raise _BadParamsError("'mode' must be 'auto' or 'supervised'")
        max_redo = int(args.get("max_redo", 3))
        cross_task = bool(args.get("cross_task", True))

        cfg = self.config.model_copy(deep=True)
        if not cross_task:
            cfg.runtime.cross_task_memory = False

        tid = new_task_id()
        td = TaskDir(root=self.root, task_id=tid)
        td.init()

        # Late import to avoid circulars and to honor stateless leaf principle.
        from agent_loop.orchestrator import Orchestrator

        orch = Orchestrator(td, cfg)
        result = orch.run(task=task, max_cycles=cycles, mode=mode, max_redo=max_redo)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _tool_list(self, args: dict[str, Any]) -> str:
        root = Path(args.get("root") or self.root)
        tasks = list_tasks(root)
        rows = [
            {
                "task_id": t.task_id,
                "path": str(t.path),
                "modified": datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M"),
            }
            for t in tasks
        ]
        return json.dumps({"root": str(root), "tasks": rows}, ensure_ascii=False, indent=2)

    def _tool_status(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise _BadParamsError("'task_id' is required")
        root = Path(args.get("root") or self.root)
        td = TaskDir(root=root, task_id=task_id)
        if not td.path.exists():
            raise _NotFoundError(f"task not found: {task_id}")
        cp = td.load_latest_checkpoint()
        latest = {
            "task_id": task_id,
            "path": str(td.path),
            "checkpoint": cp,
        }
        # Best-known score from solution.json / best_solution.json.
        for art in ("best_solution.json", "solution.json"):
            if td.has_artifact(art):
                obj = td.read_artifact(art)
                if isinstance(obj, dict):
                    ws = obj.get("weighted_score", obj.get("score"))
                    if isinstance(ws, (int, float)):
                        latest["weighted_score"] = float(ws)
                        latest["score_source"] = art
                        break
        # Latest judge action (if any).
        if td.has_artifact("judge_result.json"):
            jr = td.read_artifact("judge_result.json")
            if isinstance(jr, dict):
                latest["judge_action"] = jr.get("action")
                latest["judge_better"] = jr.get("better")
        return json.dumps(latest, ensure_ascii=False, indent=2)

    def _tool_resume(self, args: dict[str, Any]) -> str:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise _BadParamsError("'task_id' is required")
        cycles = int(args.get("cycles", 5))
        max_redo = int(args.get("max_redo", 3))
        td = TaskDir(root=self.root, task_id=task_id)
        if not td.path.exists():
            raise _NotFoundError(f"task not found: {task_id}")
        task_text = td.task_md_path().read_text(encoding="utf-8") if td.task_md_path().exists() else ""
        if not task_text.strip():
            raise _BadParamsError(f"task.md is empty for {task_id}")

        from agent_loop.orchestrator import Orchestrator

        orch = Orchestrator(td, self.config)
        result = orch.run(task=task_text, max_cycles=cycles, mode="auto", max_redo=max_redo)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _tool_bench(self, args: dict[str, Any]) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise _BadParamsError("'name' is required")
        # Reuse CLI helper to keep parity.
        from agent_loop.cli import _bench_to_task_md, _find_benchmarks_dir
        import yaml

        bench_dir = _find_benchmarks_dir()
        if bench_dir is None:
            raise _NotFoundError("benchmarks/ directory not found on this host")
        path = bench_dir / f"{name}.yaml"
        if not path.exists():
            raise _NotFoundError(f"benchmark not found: {name}")
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        task_text = _bench_to_task_md(spec)
        budget = spec.get("budget") or {}
        cycles = int(args.get("cycles") or budget.get("max_cycles", 5))
        max_redo = int(budget.get("max_redo", 3))

        tid = new_task_id()
        td = TaskDir(root=self.root, task_id=f"bench-{name}-{tid}")
        td.init()
        td.task_md_path().write_text(task_text, encoding="utf-8")
        crit = spec.get("success_criteria") or []
        if crit:
            from agent_loop.verify_engine import yaml_to_rubric

            td.write_artifact("rubric.json", yaml_to_rubric(crit))

        from agent_loop.orchestrator import Orchestrator

        orch = Orchestrator(td, self.config)
        result = orch.run(task=task_text, max_cycles=cycles, mode="auto", max_redo=max_redo)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _tool_memory_show(self, args: dict[str, Any]) -> str:
        if not self.config.runtime.cross_task_memory:
            raise _PrivacyError("cross-task memory is disabled (runtime.cross_task_memory=False)")
        limit = int(args.get("limit", 50))
        gdir = Path(self.config.runtime.cross_task_memory_dir).expanduser()
        p = gdir / "patterns.md"
        if not p.exists():
            return json.dumps({"path": str(p), "lines": []}, ensure_ascii=False, indent=2)
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        tail = lines[-limit:] if limit > 0 else lines
        return json.dumps(
            {"path": str(p), "lines": tail, "total_lines": len(lines)},
            ensure_ascii=False,
            indent=2,
        )

    # ------------------------------------------------------------------
    # resources
    # ------------------------------------------------------------------
    def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"resources": RESOURCE_SPECS}

    def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("agent-loop://"):
            raise _BadParamsError("'uri' must be 'agent-loop://...'")
        body = uri[len("agent-loop://"):]

        if body == "global/patterns":
            if not self.config.runtime.cross_task_memory:
                raise _PrivacyError(
                    "cross-task memory is disabled (runtime.cross_task_memory=False)"
                )
            gdir = Path(self.config.runtime.cross_task_memory_dir).expanduser()
            p = gdir / "patterns.md"
            if not p.exists():
                raise _NotFoundError(f"patterns.md not found at {p}")
            return _resource_text(uri, p.read_text(encoding="utf-8"), "text/markdown")

        # task scope: task/{id}/{kind}
        if not body.startswith("task/"):
            raise _BadParamsError(f"unsupported uri: {uri}")
        parts = body.split("/", 2)
        if len(parts) < 3:
            raise _BadParamsError(f"unsupported uri: {uri}")
        _, task_id, kind = parts
        if not task_id:
            raise _BadParamsError("missing task_id in uri")
        td = TaskDir(root=self.root, task_id=task_id)
        if not td.path.exists():
            raise _NotFoundError(f"task not found: {task_id}")

        if kind == "solution":
            for fname in ("best_solution.py", "solution.py"):
                p = td.workspace_path() / fname
                if p.exists():
                    return _resource_text(uri, p.read_text(encoding="utf-8"), "text/x-python")
            raise _NotFoundError(f"no solution.py/best_solution.py in {td.path}")

        if kind == "memory":
            ep = td.memory_dir() / "episodic.md"
            cf = td.memory_dir() / "core_facts.md"
            ep_text = ep.read_text(encoding="utf-8") if ep.exists() else ""
            cf_text = cf.read_text(encoding="utf-8") if cf.exists() else ""
            content = (
                f"# Episodic\n{ep_text.strip() or '(none)'}\n\n"
                f"# Core Facts\n{cf_text.strip() or '(none)'}\n"
            )
            return _resource_text(uri, content, "text/markdown")

        if kind == "metrics":
            p = td.path / "telemetry" / "metrics.jsonl"
            if not p.exists():
                raise _NotFoundError(f"metrics.jsonl not found in {td.path}")
            return _resource_text(uri, p.read_text(encoding="utf-8"), "application/x-jsonlines")

        raise _BadParamsError(f"unsupported task resource kind: {kind}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _PrivacyError(Exception):
    """Raised when a request hits a privacy-disabled resource."""


class _NotFoundError(Exception):
    """Raised when a task / artifact / file is missing."""


class _BadParamsError(Exception):
    """Raised on malformed tool / resource params."""


def _wrap_text(text: str) -> dict[str, Any]:
    """MCP standard tool reply shape."""
    return {"content": [{"type": "text", "text": text}]}


def _resource_text(uri: str, text: str, mime: str) -> dict[str, Any]:
    """MCP standard resource reply shape (text variant)."""
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


__all__ = ["Handlers", "TOOL_SPECS", "RESOURCE_SPECS"]
