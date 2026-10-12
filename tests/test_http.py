from __future__ import annotations

import pytest

from devo_mcp.cli import main, token_warning
from devo_mcp.http_app import BearerAuth, build_http_app
from devo_mcp.server import build_server
from devo_mcp.settings import Settings

KEY = "k" * 32


async def asgi_get(app, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers], "root_path": "",
             "scheme": "http", "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1)}
    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize("header,status", [(None, 401), ("Bearer wrong", 401), ("Basic " + KEY, 401),
                                           ("Bearer " + KEY, 200), ("bearer " + KEY, 200)])
async def test_bearer_auth(header, status):
    app = BearerAuth(ok_app, KEY)
    assert await asgi_get(app, "/", [("Authorization", header)] if header else []) == status


async def test_healthz_is_open_and_mcp_is_guarded(tmp_path):
    app = build_http_app(build_server(Settings(workdir=tmp_path)), KEY, "0.0.0.0")
    assert await asgi_get(app, "/healthz") == 200
    assert await asgi_get(app, "/mcp/") == 401


def test_http_refuses_to_start_without_key(monkeypatch, capsys):
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("DEVO_MCP_ALLOW_NO_AUTH", raising=False)
    assert main(["--transport", "http"]) == 1
    assert "MCP_API_KEY" in capsys.readouterr().err


def test_http_refuses_short_key(monkeypatch, capsys):
    monkeypatch.setenv("MCP_API_KEY", "short")
    assert main(["--transport", "http"]) == 1


def test_token_warning(tmp_path):
    assert token_warning({"DEVO_TOKEN": "x"}) is None
    assert "missing or unreadable" in token_warning({"DEVO_ENV_FILE": str(tmp_path / "nope")})
    f = tmp_path / "env"
    f.write_text("DEVO_TOKEN=x\n")
    assert token_warning({"DEVO_ENV_FILE": str(f)}) is None


def test_settings_from_env(tmp_path):
    s = Settings.from_env({"DEVO_MCP_ALLOW_WRITES": "true", "DEVO_MCP_WORKDIR": str(tmp_path),
                           "MCP_API_KEY": KEY, "DEVO_MCP_ALLOWED_HOSTS": "a.example, b.example"})
    assert s.allow_writes and s.workdir == tmp_path and s.api_key == KEY
    assert s.allowed_hosts == ("a.example", "b.example")
    assert Settings.from_env({}).allow_writes is False


async def test_websocket_is_refused():
    sent = []

    async def app(scope, receive, send):
        raise AssertionError("the inner app must not see a websocket")

    async def send(msg):
        sent.append(msg)
    await BearerAuth(app, "k" * 32)({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("env,msg", [
    ({"DEVO_MCP_ALLOW_WRITES": "ture"}, "true or false"),
    ({"DEVO_MCP_SOFT_TIMEOUT_SECONDS": "-1"}, "negative"),
    ({"DEVO_MCP_WORK_TTL_HOURS": "1", "DEVO_MCP_MAX_JOB_SECONDS": "7200"}, "longer than"),
])
def test_bad_settings_are_refused(env, msg):
    with pytest.raises(SystemExit, match=msg):
        Settings.from_env(env)
