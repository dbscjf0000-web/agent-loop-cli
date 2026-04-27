"""Unit tests for the JSON-RPC 2.0 wire helpers (v0.5)."""
from __future__ import annotations

import json

import pytest

from agent_loop.mcp.protocol import (
    ERR_INVALID_PARAMS,
    ParseError,
    Request,
    Response,
    make_error,
    parse_request,
    serialize_response,
)


def test_parse_request_basic() -> None:
    line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}})
    req = parse_request(line)
    assert req.method == "tools/list"
    assert req.id == 7
    assert req.params == {}
    assert req.jsonrpc == "2.0"


def test_parse_request_string_id_and_no_params() -> None:
    """id may be a string; params is optional."""
    line = json.dumps({"jsonrpc": "2.0", "id": "alpha", "method": "initialize"})
    req = parse_request(line)
    assert req.id == "alpha"
    assert req.params is None


def test_parse_request_rejects_invalid_json() -> None:
    with pytest.raises(ParseError):
        parse_request("{not valid json")


def test_parse_request_rejects_missing_method() -> None:
    line = json.dumps({"jsonrpc": "2.0", "id": 1})
    with pytest.raises(ParseError):
        parse_request(line)


def test_parse_request_rejects_bad_id_type() -> None:
    line = json.dumps({"jsonrpc": "2.0", "id": [1, 2], "method": "ping"})
    with pytest.raises(ParseError):
        parse_request(line)


def test_serialize_response_result() -> None:
    resp = Response(id=42, result={"ok": True})
    obj = json.loads(serialize_response(resp))
    assert obj["jsonrpc"] == "2.0"
    assert obj["id"] == 42
    assert obj["result"] == {"ok": True}
    assert "error" not in obj


def test_serialize_response_error() -> None:
    resp = make_error(99, ERR_INVALID_PARAMS, "bad", data={"hint": "x"})
    obj = json.loads(serialize_response(resp))
    assert obj["id"] == 99
    assert obj["error"]["code"] == ERR_INVALID_PARAMS
    assert obj["error"]["message"] == "bad"
    assert obj["error"]["data"] == {"hint": "x"}
    assert "result" not in obj
