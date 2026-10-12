"""devo-mcp command line: run the server over stdio (default) or streamable HTTP."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .settings import Settings, env_int, token_warning  # noqa: F401 (re-exported)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devo-mcp", description="MCP server for the Devo SIEM")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("DEVO_MCP_TRANSPORT", "stdio"))
    ap.add_argument("--host", default=os.environ.get("DEVO_MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=env_int("DEVO_MCP_PORT", 8000))
    ap.add_argument("--path", default=os.environ.get("DEVO_MCP_PATH", "/mcp"), help="HTTP path of the MCP endpoint")
    ap.add_argument("--json-response", action="store_true",
                    help="HTTP: plain JSON responses instead of SSE streams (no progress notifications)")
    ap.add_argument("--version", action="version", version=f"devo-mcp {__version__}")
    a = ap.parse_args(argv)

    settings = Settings.from_env()
    if warning := token_warning():
        print(warning, file=sys.stderr)
    from .server import build_server

    if a.transport == "stdio":
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        import anyio
        mcp = build_server(settings)

        async def serve():
            mcp.devo_warmer.start()
            await mcp.run_stdio_async()
        anyio.run(serve)
        return 0

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.api_key and not settings.allow_no_auth:
        print("refusing to serve HTTP without MCP_API_KEY: the server holds a Devo token. Set MCP_API_KEY "
              "(clients send Authorization: Bearer <key>), or DEVO_MCP_ALLOW_NO_AUTH=true only when an "
              "authenticating proxy is in front.", file=sys.stderr)
        return 1
    if settings.api_key and len(settings.api_key) < 24:
        print("MCP_API_KEY is too short: use at least 24 random characters (e.g. `openssl rand -hex 32`)",
              file=sys.stderr)
        return 1

    import uvicorn

    from .http_app import build_http_app
    mcp = build_server(settings)
    app = build_http_app(mcp, settings.api_key, a.host, a.path, settings.allowed_hosts, a.json_response,
                         on_startup=mcp.devo_warmer.start)
    print(f"devo-mcp {__version__}: http://{a.host}:{a.port}{a.path} (writes "
          f"{'enabled' if settings.allow_writes else 'disabled'}, auth {'bearer' if settings.api_key else 'NONE'})",
          file=sys.stderr)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", proxy_headers=True, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
