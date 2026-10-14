"""Opt-in "tool errors as text" mode (MCP_TOOL_ERRORS_AS_TEXT).

Microsoft Foundry Agent Service ends the whole agent run with `tool_user_error` when a tool result
has `isError: true`, so the model never sees the error and can't fix its arguments. With this mode
on, every `tools/call` error result goes out as `isError: false` with one text block,
`ERROR: <the original message>`, which the model can read and act on.

It is server middleware, so it sees the final wire result of every `tools/call`: ToolError from a
tool (engine failures included), unexpected exceptions, argument validation failures and unknown
tool names. A tool with an outputSchema also gets structuredContent that satisfies it, because
strict clients reject an `isError: false` result without it. Off by default, and when off the
middleware isn't installed.
"""
from __future__ import annotations

import logging
from typing import Any

import jsonschema
from mcp.server.context import (
    CallNext,
    HandlerResult,
    ServerMiddleware,
    ServerRequestContext,
)
from mcp.server.mcpserver import MCPServer

log = logging.getLogger("devo_mcp.tool_errors")

ENV = "MCP_TOOL_ERRORS_AS_TEXT"
PREFIX = "ERROR: "


def structured_error(schema: dict[str, Any], text: str) -> dict[str, Any] | None:
    """The first of a few small objects carrying `text` that `schema` accepts, else None."""
    for candidate in ({"result": text}, {"error": text}, {}):
        try:
            jsonschema.validate(candidate, schema)
        except jsonschema.ValidationError:
            continue
        return candidate
    return None


class ToolErrorsAsText(ServerMiddleware[Any]):
    def __init__(self, mcp: MCPServer):
        self.mcp = mcp

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/call" or not isinstance(result, dict) or result.get("isError") is not True:
            return result
        name = (ctx.params or {}).get("name")
        text = PREFIX + "\n".join(b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text")
        out = {**result, "content": [{"type": "text", "text": text}], "isError": False}
        out.pop("structuredContent", None)
        schema = next((t.output_schema for t in await self.mcp.list_tools() if t.name == name), None)
        if schema is not None:
            if (structured := structured_error(schema, text)) is not None:
                out["structuredContent"] = structured
            else:
                log.warning("tool %r: no structuredContent fits its outputSchema; sending text only", name)
        # The tool name only: arguments and the message can hold the caller's data.
        log.warning("tool %r failed; returned the error as text (isError=false)", name)
        return out
