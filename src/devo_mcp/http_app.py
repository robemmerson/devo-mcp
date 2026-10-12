"""Streamable HTTP app: the MCP endpoint behind a bearer API key, plus an open /healthz."""
from __future__ import annotations

import hmac

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__


class BearerAuth:
    """Rejects HTTP requests without `Authorization: Bearer <key>` (constant-time compare), and any
    other connection type (websocket) outright: the MCP endpoint is plain HTTP."""

    def __init__(self, app: ASGIApp, key: str):
        self.app = app
        self.key = key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, supplied = header.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), self.key):
                resp = JSONResponse({"error": "unauthorized", "detail": "send Authorization: Bearer <MCP_API_KEY>"},
                                    status_code=401, headers={"WWW-Authenticate": 'Bearer realm="devo-mcp"'})
                await resp(scope, receive, send)
                return
        elif scope["type"] != "lifespan":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


class WithHealthz:
    """Answers GET /healthz itself (no auth, for load balancers); everything else, including the
    lifespan events the MCP app needs, goes to the wrapped app."""

    def __init__(self, app: ASGIApp, on_startup=None):
        self.app = app
        self.on_startup = on_startup
        self.ok = PlainTextResponse(f"ok devo-mcp {__version__}\n")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await self.ok(scope, receive, send)
            return
        if scope["type"] == "lifespan" and self.on_startup is not None:
            inner_receive = receive

            async def receive():  # run on_startup (e.g. the cache warmer) when the server starts
                msg = await inner_receive()
                if msg["type"] == "lifespan.startup":
                    self.on_startup()
                return msg
        await self.app(scope, receive, send)


def build_http_app(mcp: MCPServer, api_key: str | None, host: str, path: str = "/mcp",
                   allowed_hosts: tuple[str, ...] = (), json_response: bool = False, on_startup=None) -> ASGIApp:
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts),
                                             allowed_origins=[f"https://{h}" for h in allowed_hosts]
                                             + [f"http://{h}" for h in allowed_hosts])
    # Stateless: every request stands alone, so the server survives restarts and runs behind any proxy.
    # Background jobs and workspace files live on disk, not in MCP sessions. The endpoint is served at
    # exactly `path` (no redirect to `path/`, which some clients won't follow with a POST).
    app: ASGIApp = mcp.streamable_http_app(streamable_http_path=path, stateless_http=True,
                                           json_response=json_response, transport_security=security, host=host)
    if api_key:
        app = BearerAuth(app, api_key)
    return WithHealthz(app, on_startup)
