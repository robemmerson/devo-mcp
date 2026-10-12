from __future__ import annotations

import asyncio

from mcp.client.client import Client

from devo_mcp.http_app import WithHealthz
from devo_mcp.server import build_server
from devo_mcp.settings import Settings

from test_server import FakeEngine, call, fake  # noqa: F401 (fixture)


def warm_server(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("DEVO_TOKEN", "x")  # the warmer stays off without a token
    return build_server(Settings(workdir=tmp_path / "work", cache_warmup=True, cache_warmup_delay=0,
                                 cache_check_hours=0, **kw))


async def wait_state(warmer, states=("idle", "failed")):
    for _ in range(200):
        if warmer.status["state"] in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(warmer.status)


async def test_warmer_builds_cache_and_reports(tmp_path, monkeypatch, fake):
    fake.reply["cache"] = (0, "", "# cache: 388 tables profiled\n")
    mcp = warm_server(tmp_path, monkeypatch, cache_profile="40")
    async with Client(mcp) as c:
        mcp.devo_warmer.start()
        await wait_state(mcp.devo_warmer)
        assert fake.calls[0] == ["cache", "--profile=40", "--parallel=6", "--", "build"]
        err, res = await call(c, "devo_check", {})
    w = res["server"]["cache_warmup"]
    assert w["state"] == "idle" and w["last_result"] == "done" and "388 tables" in w["last_message"]


async def test_manual_build_reuses_running_warmup(tmp_path, monkeypatch, fake):
    gate = asyncio.Event()
    orig = FakeEngine.__call__

    async def slow(self, runner, args, timeout, stdin=None):
        if args[0] == "cache":
            await gate.wait()
        return await orig(self, runner, args, timeout, stdin)
    monkeypatch.setattr(FakeEngine, "__call__", slow)
    mcp = warm_server(tmp_path, monkeypatch)
    async with Client(mcp) as c:
        mcp.devo_warmer.start()
        await wait_state(mcp.devo_warmer, ("running",))
        err, res = await call(c, "devo_cache", {"action": "build"})
        assert not err and res["job_id"] == mcp.devo_warmer.status["job_id"] and "warm-up" in res["note"]
        gate.set()
        await wait_state(mcp.devo_warmer)
    assert sum(1 for a in fake.calls if a[0] == "cache") == 1


async def test_warmer_disabled(tmp_path, monkeypatch, fake):
    monkeypatch.delenv("DEVO_TOKEN", raising=False)
    monkeypatch.setenv("DEVO_ENV_FILE", str(tmp_path / "missing"))
    mcp = build_server(Settings(workdir=tmp_path / "w", cache_warmup=True, cache_warmup_delay=0))
    mcp.devo_warmer.start()
    await asyncio.sleep(0.05)
    assert mcp.devo_warmer.status == {"enabled": False, "state": "disabled", "profile": "all", "every_hours": 24,
                                      "reason": "no Devo token"} and not fake.calls
    off = build_server(Settings(workdir=tmp_path / "w2"))  # cache_warmup defaults off outside from_env
    assert off.devo_warmer.status["state"] == "disabled"


async def test_lifespan_startup_triggers_hook():
    started = []

    async def app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"

    async def receive():
        return {"type": "lifespan.startup"}

    await WithHealthz(app, on_startup=lambda: started.append(1))({"type": "lifespan"}, receive, None)
    assert started == [1]


def test_settings_cache_env():
    s = Settings.from_env({"DEVO_MCP_CACHE_PROFILE": "60", "DEVO_MCP_CACHE_CHECK_HOURS": "12"})
    assert s.cache_warmup and s.cache_profile == "60" and s.cache_check_hours == 12
    assert Settings.from_env({"DEVO_MCP_CACHE_WARMUP": "false"}).cache_warmup is False


async def test_job_output_streams_to_files(tmp_path):
    import sys
    from devo_mcp.engine_runner import EngineRunner
    from devo_mcp.workspace import Workspace
    runner = EngineRunner(sys.executable, Workspace(tmp_path / "w"))
    st = runner.start_job("t", lambda d: ["-h"], 60)  # the real engine, offline: argparse help
    await runner.wait_job(st["job_id"])
    final, d = runner.job_status(st["job_id"])
    assert final["state"] == "done" and "usage" in (d / "stdout.txt").read_text()
