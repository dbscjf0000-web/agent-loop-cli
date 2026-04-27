"""MCP server main loop.

Currently only the **stdio** transport is implemented (each newline-delimited
line on stdin is one JSON-RPC 2.0 request, each response is one line on
stdout). HTTP transport is reserved for v0.5.x.

The loop is intentionally simple — synchronous, single-threaded, no
buffering. Servers stay alive until stdin closes (EOF) or the parent
client kills them.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Iterable

from agent_loop.config import Config
from agent_loop.mcp.handlers import Handlers
from agent_loop.mcp.protocol import (
    ERR_INTERNAL,
    ERR_INVALID_REQUEST,
    ERR_PARSE,
    ParseError,
    make_error,
    parse_request,
    serialize_response,
)


def serve_stdio(
    config: Config,
    root: Path,
    *,
    stdin: IO[str] | Iterable[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Run the JSON-RPC 2.0 loop on stdin/stdout. Returns process exit code.

    ``stdin`` / ``stdout`` are injectable for tests — production callers pass
    ``None`` and we use the real ``sys.stdin`` / ``sys.stdout``.
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout

    handlers = Handlers(config=config, root=Path(root))

    for raw in stdin:
        line = raw.rstrip("\r\n")
        if not line.strip():
            # Skip blank keepalive lines silently.
            continue

        # 1) parse
        try:
            req = parse_request(line)
        except ParseError as e:
            # No id available — JSON-RPC says null id is the standard reply
            # for parse errors when the request id can't be recovered.
            resp = make_error(None, ERR_PARSE, f"parse error: {e}")
            _write(stdout, serialize_response(resp))
            continue

        # 2) bare-bones JSON-RPC 2.0 validation we couldn't catch in parse_request
        if req.jsonrpc != "2.0":
            resp = make_error(req.id, ERR_INVALID_REQUEST, "jsonrpc must be '2.0'")
            _write(stdout, serialize_response(resp))
            continue

        # 3) dispatch to handlers (handlers themselves never raise — they wrap
        #    every error into a Response).
        try:
            resp = handlers.dispatch(req.method, req.params, req.id)
        except Exception as e:  # pragma: no cover - defensive
            resp = make_error(req.id, ERR_INTERNAL, f"internal error: {e}")

        # 4) Notifications (id=None per JSON-RPC 2.0) get no reply.
        if req.id is None:
            continue
        _write(stdout, serialize_response(resp))

    return 0


def _write(stdout: IO[str], line: str) -> None:
    stdout.write(line + "\n")
    try:
        stdout.flush()
    except Exception:  # pragma: no cover - some test stubs lack flush
        pass


__all__ = ["serve_stdio"]
