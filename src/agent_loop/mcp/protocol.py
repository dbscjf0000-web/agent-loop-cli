"""JSON-RPC 2.0 wire format for the MCP server (v0.5).

stdlib only. Each line on stdio is a single JSON object — request from
client, response from server. No batching in v0.5.0 (spec allows arrays
but no MCP client we target uses them; a v0.5.x bump can add support).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Standard JSON-RPC 2.0 error codes (https://www.jsonrpc.org/specification)
# ---------------------------------------------------------------------------
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# agent-loop custom range (-32000..-32099 reserved for server-defined errors).
ERR_PRIVACY_DISABLED = -32000  # cross_task=False but global resource requested
ERR_NOT_FOUND = -32001  # task / artifact / resource missing
ERR_TOOL_FAILED = -32002  # tool handler raised — wraps the inner exception


# ---------------------------------------------------------------------------
# data classes — one Request / one Response per line
# ---------------------------------------------------------------------------

@dataclass
class Request:
    """Parsed JSON-RPC 2.0 request.

    ``id`` may be ``None`` for notifications (no response expected). The MCP
    methods we serve all carry an id, but we tolerate notifications by simply
    not emitting a response when ``id is None``.
    """

    method: str = ""
    id: str | int | None = None
    params: dict[str, Any] | None = None
    jsonrpc: str = "2.0"


@dataclass
class Response:
    """JSON-RPC 2.0 response. Exactly one of ``result`` / ``error`` is set."""

    id: str | int | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            out["error"] = self.error
        else:
            out["result"] = self.result
        return out


# ---------------------------------------------------------------------------
# parsers / serializers
# ---------------------------------------------------------------------------

class ParseError(ValueError):
    """Raised when a stdin line is not a valid JSON-RPC 2.0 request."""


def parse_request(line: str) -> Request:
    """Parse one stdin line into a Request.

    Raises ``ParseError`` for malformed JSON or missing ``method`` field
    (caller should respond with ``ERR_PARSE`` / ``ERR_INVALID_REQUEST``).
    """
    line = line.strip()
    if not line:
        raise ParseError("empty line")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise ParseError(f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ParseError("request must be a JSON object")
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise ParseError("missing or invalid 'method'")
    rid = obj.get("id")
    # JSON-RPC: id may be string, integer, or null. Reject other types.
    if rid is not None and not isinstance(rid, (str, int)):
        raise ParseError("'id' must be string, number, or null")
    params = obj.get("params")
    if params is not None and not isinstance(params, dict):
        raise ParseError("'params' must be an object when present")
    return Request(method=method, id=rid, params=params, jsonrpc=str(obj.get("jsonrpc", "2.0")))


def serialize_response(resp: Response) -> str:
    """Serialize a Response to one stdout line (no trailing newline)."""
    return json.dumps(resp.to_dict(), ensure_ascii=False)


def make_error(rid: str | int | None, code: int, message: str, data: Any = None) -> Response:
    """Build a Response carrying a JSON-RPC error payload."""
    err: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return Response(id=rid, error=err)


__all__ = [
    "ERR_PARSE",
    "ERR_INVALID_REQUEST",
    "ERR_METHOD_NOT_FOUND",
    "ERR_INVALID_PARAMS",
    "ERR_INTERNAL",
    "ERR_PRIVACY_DISABLED",
    "ERR_NOT_FOUND",
    "ERR_TOOL_FAILED",
    "ParseError",
    "Request",
    "Response",
    "make_error",
    "parse_request",
    "serialize_response",
]
