"""MCP_TOOL_ERRORS_AS_TEXT: tool errors returned as `ERROR: …` text with isError false."""
from __future__ import annotations

import logging

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from test_server import FakeEngine, fake, server  # noqa: F401 (fake is a fixture)

from devo_mcp.engine_runner import EngineRunner
from devo_mcp.settings import Settings
from devo_mcp.tool_errors import structured_error

SECRET = "s3cr3t-linq-value"

FAILURES = [
    pytest.param("devo_query", {"linq": "from t select x"}, "hint: list fields with devo_schema", id="engine-error"),
    pytest.param("devo_fields", {"table": "bad;table"}, "is not a table name", id="tool-error"),
    pytest.param("devo_query", {}, "linq", id="missing-argument"),
    pytest.param("devo_query", {"linq": "from t", "limit": "many"}, "valid integer", id="wrong-type"),
    pytest.param("devo_nope", {}, "devo_nope", id="unknown-tool"),
]


def text(r) -> str:
    assert len(r.content) == 1
    return r.content[0].text


def with_test_tools(mcp):
    """Add tools with an outputSchema: a wrapped `{"result": …}` one and a mapping one."""
    @mcp.tool(structured_output=True)
    def typed(n: int) -> str:
        raise ToolError(f"typed failed for {n}")

    @mcp.tool(structured_output=True)
    def mapping(n: int) -> dict[str, str]:
        raise ToolError("mapping failed")
    return mcp


@pytest.fixture
def engine_error(fake):
    fake.reply["query"] = (2, "", "ERROR: Unknown identifier `x`\nhint: list fields with devo_schema\n")
    return fake


async def run(tmp_path, on: bool, name: str, args: dict):
    async with Client(server(tmp_path, tool_errors_as_text=on)) as c:
        return await c.call_tool(name, args)


@pytest.mark.parametrize("name,args,message", FAILURES)
async def test_flag_off_keeps_is_error(tmp_path, engine_error, name, args, message):
    r = await run(tmp_path, False, name, args)
    assert r.is_error is True
    assert message in text(r) and not text(r).startswith("ERROR: ")


@pytest.mark.parametrize("name,args,message", FAILURES)
async def test_flag_on_returns_error_as_text(tmp_path, engine_error, name, args, message):
    off = await run(tmp_path, False, name, args)
    on = await run(tmp_path, True, name, args)
    assert on.is_error is False
    assert text(on) == "ERROR: " + text(off)  # the full original message, hints included
    assert message in text(on)


async def test_flag_on_unexpected_exception(tmp_path, monkeypatch):
    async def crash(self, *a, **kw):
        raise RuntimeError("engine pipe broke")
    monkeypatch.setattr(EngineRunner, "_exec", crash)
    off = await run(tmp_path, False, "devo_query", {"linq": "from t"})
    on = await run(tmp_path, True, "devo_query", {"linq": "from t"})
    assert off.is_error is True
    assert on.is_error is False and text(on) == "ERROR: " + text(off)


async def test_flag_on_success_unchanged(tmp_path, fake):
    fake.reply["query"] = (0, '[{"n": 5}]', "# 1 rows\n")
    off = await run(tmp_path, False, "devo_query", {"linq": "from t"})
    on = await run(tmp_path, True, "devo_query", {"linq": "from t"})
    assert on.is_error is False and on.content == off.content and on.structured_content == off.structured_content


@pytest.mark.parametrize("name,message", [("typed", "typed failed for 3"), ("mapping", "mapping failed")])
async def test_flag_on_output_schema_tool_passes_strict_client(tmp_path, name, message):
    # Client validates structuredContent against the tool's outputSchema whenever isError is false,
    # so getting a result back at all means a strict client accepted it.
    mcp = with_test_tools(server(tmp_path, tool_errors_as_text=True))
    async with Client(mcp) as c:
        assert {t.name: t.output_schema for t in (await c.list_tools()).tools}[name] is not None
        r = await c.call_tool(name, {"n": 3})
    assert r.is_error is False
    assert text(r).startswith("ERROR: ") and message in text(r)
    assert r.structured_content == {"result": text(r)}


def test_unsatisfiable_schema_gets_no_structured_content():
    assert structured_error({"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
                            "ERROR: x") is None


async def test_flag_on_logs_tool_name_not_arguments(tmp_path, engine_error, caplog):
    with caplog.at_level(logging.WARNING, logger="devo_mcp.tool_errors"):
        await run(tmp_path, True, "devo_query", {"linq": f"from t select {SECRET}"})
    records = [r for r in caplog.records if r.name == "devo_mcp.tool_errors"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "devo_query" in records[0].getMessage()
    assert SECRET not in caplog.text


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("1", True), ("yes", True),
                                            ("on", True), ("false", False), ("0", False), ("no", False),
                                            ("", False), (None, False)])
def test_env_parsing(value, expected):
    env = {} if value is None else {"MCP_TOOL_ERRORS_AS_TEXT": value}
    assert Settings.from_env(env).tool_errors_as_text is expected


def test_env_typo_refused():
    with pytest.raises(SystemExit, match="MCP_TOOL_ERRORS_AS_TEXT"):
        Settings.from_env({"MCP_TOOL_ERRORS_AS_TEXT": "ture"})
