"""Model Context Protocol (MCP) server for agent-loop (v0.5).

Exposes the R->P->I->V->J loop as a JSON-RPC 2.0 server so other AI
clients (Claude Code, Cursor, OpenCode, etc.) can drive tasks through the
standard MCP `tools/call` / `resources/read` methods.

This is a self-contained implementation built on stdlib only — no `mcp`
SDK dependency. See ``docs/plan-v0.5.md`` for design rationale.
"""
from __future__ import annotations

# MCP protocol version we advertise during `initialize`. Aligned with the
# 2024-11-05 spec (Anthropic's first stable release). Bumped only when the
# wire-format compatibility changes; capabilities live in the handshake.
PROTOCOL_VERSION = "2024-11-05"

# Server identity reported in `initialize` response.
SERVER_NAME = "agent-loop"
SERVER_VERSION = "0.5.0"

__all__ = ["PROTOCOL_VERSION", "SERVER_NAME", "SERVER_VERSION"]
