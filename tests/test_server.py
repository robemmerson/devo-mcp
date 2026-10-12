"""Offline tests: the engine subprocess is replaced by a fake that records arguments."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os

import pytest
from mcp.client.client import Client

from devo_mcp import references
from devo_mcp.engine_runner import ENGINE, EngineResult, EngineRunner, engine_env
from devo_mcp.server import build_server
from devo_mcp.settings import Settings


def _engine_parser():
    spec = importlib.util.spec_from_file_location("devo_engine", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_parser()


ENGINE_PARSER = _engine_parser()


def parses(args: list[str]) -> None:
    """Fail the test if the real engine's argparse would reject what the server built."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            ENGINE_PARSER.parse_args(args)
    except SystemExit:
        pytest.fail(f"the engine rejects {args}: {err.getvalue().strip()}")


class FakeEngine:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.reply: dict[str, tuple[int, str, str]] = {}

    async def __call__(self, runner, args, timeout, stdin=None):
        parses(args)
        self.calls.append(args)
        code, out, err = self.reply.get(args[0], (0, "", ""))
        report = next((a.split("=", 1)[1] for a in args if a.startswith("--out=")), None)
        if report and code == 0:  # behave like the engine: write the report
            with open(report, "w") as f:
                f.write('{"written": true}')
        return EngineResult(args, code, out, err, 0.0)


@pytest.fixture
def fake(monkeypatch):
    f = FakeEngine()
    monkeypatch.setattr(EngineRunner, "_exec", lambda self, a, t, s=None, **kw: f(self, a, t, s))
    return f


def server(tmp_path, **kw):
    return build_server(Settings(workdir=tmp_path / "work", **kw))


async def call(c, name, args):
    r = await c.call_tool(name, args)
    text = "".join(getattr(x, "text", "") for x in r.content)
    try:
        return r.is_error, json.loads(text)
    except json.JSONDecodeError:
        return r.is_error, text


def test_engine_is_vendored():
    assert ENGINE.is_file()
    assert (ENGINE.parent.parent / "references" / "linq-traps.md").is_file()
    assert "credential-attacks" in references.bundled_specs()


def test_engine_env_drops_mcp_key(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "secret")
    monkeypatch.setenv("DEVO_REGION", "eu")
    env = engine_env()
    assert "MCP_API_KEY" not in env and env["DEVO_REGION"] == "eu"


async def test_write_tools_hidden_by_default(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "devo_query" in names
    assert not names & {"devo_alert_comment", "devo_board_push", "devo_board_delete"}


async def test_write_tools_present_when_allowed(tmp_path, fake):
    async with Client(server(tmp_path, allow_writes=True)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert tools["devo_board_delete"].annotations.destructive_hint is True
    assert tools["devo_query"].annotations.read_only_hint is True


async def test_query_args_and_json_result(tmp_path, fake):
    fake.reply["query"] = (0, '[{"kind": "host", "n": 5}]', "# 1 rows, range a → b UTC\n")
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_query", {"linq": "from t select count()", "start": "2h", "sort": "-n"})
    assert not err
    assert res["result"] == [{"kind": "host", "n": 5}]
    assert res["messages"] == ["# 1 rows, range a → b UTC"]
    args = fake.calls[0]
    assert args[0] == "query" and args[-2:] == ["--", "from t select count()"]
    assert {"--from=2h", "--to=now", "--sort=-n", "--format=json"} <= set(args)
    assert "dir" not in res  # inline queries leave no empty workspace dir
    assert os.listdir(tmp_path / "work") == []


async def test_query_save_returns_workspace_file(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_query", {"linq": "from t", "save": True})
    assert not err and res["files"] == [res["dir"] + "/rows.jsonl"], res
    assert str(tmp_path) not in res["command"]  # server paths are shown workspace-relative


async def test_engine_failure_is_tool_error(tmp_path, fake):
    fake.reply["query"] = (2, "", "ERROR: Unknown identifier `x`\nhint: list fields\n")
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_query", {"linq": "from t select x"})
    assert err and "Unknown identifier" in res and "exit 2 (Devo API error" in res


async def test_large_list_is_cut_and_saved(tmp_path, fake):
    rows = [{"i": i, "pad": "x" * 100} for i in range(500)]
    fake.reply["query"] = (0, json.dumps(rows), "")
    async with Client(server(tmp_path, max_output_chars=5000)) as c:
        err, res = await call(c, "devo_query", {"linq": "from t"})
        assert not err
        t = res["truncated"]
        assert 0 < t["items_shown"] < 500 and t["items_total"] == 500
        err, page = await call(c, "devo_workspace_read", {"path": t["full_output"], "limit": 3})
    assert not err and page["total_lines"] > 3


async def test_confirmation_token_flow(tmp_path, fake):
    fake.reply["comment"] = (0, "Alert 1: x\nPOST …", "# NOT SENT (preview)")
    params = {"alert_id": "123", "title": "t", "message": "m"}
    async with Client(server(tmp_path, allow_writes=True)) as c:
        err, prev = await call(c, "devo_alert_comment", params)
        assert not err and prev["sent"] is False and "--confirm" not in fake.calls[-1]
        err, msg = await call(c, "devo_alert_comment", params | {"confirm": True})
        assert err and "confirmation_token" in msg
        err, msg = await call(c, "devo_alert_comment", params | {"message": "other", "confirm": True,
                                                                 "confirmation_token": prev["confirmation_token"]})
        assert err and "doesn't match" in msg
        n = len(fake.calls)
        err, sent = await call(c, "devo_alert_comment", params | {"confirm": True,
                                                                  "confirmation_token": prev["confirmation_token"]})
        assert not err and sent["sent"] is True
        assert len(fake.calls) == n + 1 and fake.calls[-1][-3:] == ["--confirm", "--", "123"]
        err, msg = await call(c, "devo_alert_comment", params | {"confirm": True,
                                                                 "confirmation_token": prev["confirmation_token"]})
    assert err and "already used" in msg and len(fake.calls) == n + 1  # one approval, one change


async def test_board_push_token_bound_to_file_content(tmp_path, fake):
    async with Client(server(tmp_path, allow_writes=True)) as c:
        err, prev = await call(c, "devo_board_push", {"board": {"name": "a", "settings": {}}})
        assert not err
        path = tmp_path / "work" / prev["board_file"]
        path.write_text('{"name": "changed"}')  # edited after the preview
        err, msg = await call(c, "devo_board_push", {"board_file": prev["board_file"], "confirm": True,
                                                     "confirmation_token": prev["confirmation_token"]})
    assert err and "doesn't match" in msg


async def test_workspace_traversal_refused(tmp_path, fake):
    (tmp_path / "secret.txt").write_text("no")
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_workspace_read", {"path": "../secret.txt"})
        assert err and "outside the workspace" in msg
        err, msg = await call(c, "devo_timeline", {"dir": "/etc"})
    assert err


async def test_alert_id_must_be_numeric(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_alert", {"alert_id": "1; rm -rf /"})
    assert err and not fake.calls


async def test_background_job_lifecycle(tmp_path, fake):
    fake.reply["lag"] = (0, "table  p50\n", "# done\n")
    async with Client(server(tmp_path)) as c:
        err, job = await call(c, "devo_lag", {"background": True})
        assert not err and job["state"] == "running"
        for _ in range(50):
            err, st = await call(c, "devo_job_status", {"job_id": job["job_id"]})
            if st["state"] != "running":
                break
    assert st["state"] == "done" and st["result"] == "table  p50\n"
    assert st["report"] == {"written": True}


async def test_reference_tool(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, cat = await call(c, "devo_reference", {})
        assert not err and any(r["name"] == "linq-traps.md" for r in cat["references"])
        err, hit = await call(c, "devo_reference", {"name": "linq-traps", "grep": "weakhas", "limit": 5})
        assert not err and "weakhas" in hit["text"]
        err, msg = await call(c, "devo_reference", {"name": "../../server.py"})
    assert err


async def test_resources_and_prompts(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert "devo://reference/linq-traps.md" in uris and "devo://guide" in uris
        prompts = {p.name for p in (await c.list_prompts()).prompts}
    assert {"triage_alert", "investigate_entity", "domain_health"} <= prompts


async def test_batch_inline_jobs(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_batch", {"jobs": [{"name": "a", "query": "from t"}], "vars": {"u": "x"}})
    assert not err
    args = fake.calls[0]
    assert args[0] == "batch" and "--vars=u=x" in args
    spec = tmp_path / "work" / res["dir"] / "spec.jsonl"
    assert json.loads(spec.read_text()) == {"name": "a", "query": "from t"}


@pytest.mark.parametrize("tool,args", [
    ("devo_profile", {"tables": ["x", "--from-file=/etc/passwd"]}),
    ("devo_fields", {"table": "--map=/etc/passwd"}),
    ("devo_schema", {"table": "--help"}),
    ("devo_cache", {"action": "refresh", "args": ["--include-notes"]}),
    ("devo_cache", {"action": "clear", "args": ["../../x"]}),
    ("devo_board", {"board_id": "--json"}),
    ("devo_batch", {"jobs": [{"name": "a", "query": "q"}], "vars": {"u": "a,b"}}),
])
async def test_option_injection_refused(tmp_path, fake, tool, args):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, tool, args)
    assert err and not fake.calls


async def test_flag_like_values_stay_values(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        await call(c, "devo_query", {"linq": "--from-file=/x", "sort": "--out=/tmp/x"})
        await call(c, "devo_cache", {"action": "note_add", "args": ["--include-notes", "is a fact"]})
    q, cache = fake.calls
    assert "--sort=--out=/tmp/x" in q and q[-2:] == ["--", "--from-file=/x"]
    assert cache[-4:] == ["--", "note", "add", "--include-notes is a fact"]


async def test_inline_board_push_can_be_confirmed(tmp_path, fake):
    params = {"board": {"name": "a", "settings": {}}}
    async with Client(server(tmp_path, allow_writes=True)) as c:
        err, prev = await call(c, "devo_board_push", params)
        err, sent = await call(c, "devo_board_push", params | {"confirm": True,
                                                              "confirmation_token": prev["confirmation_token"]})
    assert not err and sent["sent"] is True and "--confirm" in fake.calls[-1]


async def test_background_jobs_get_a_long_wall_limit(tmp_path, fake, monkeypatch):
    seen = {}
    orig = EngineRunner.run

    async def spy(self, args, limit, stdin=None, job=False, log_dir=None):
        seen[args[0]] = (limit, job)
        return await orig(self, args, limit, stdin, job, log_dir)
    monkeypatch.setattr(EngineRunner, "run", spy)
    async with Client(server(tmp_path, max_call_seconds=1000, max_job_seconds=9000)) as c:
        await call(c, "devo_query", {"linq": "from t", "chunk": "1h", "timeout": 60})
        await call(c, "devo_schema", {"table": "a.b"})
    # multi-query calls run as jobs (the job pool) with the job limit: past the soft timeout they keep
    # running in the background instead of being killed, without holding a slot quick calls need
    assert seen["query"] == (9000, True) and seen["schema"] == (150, False)


async def test_slow_call_becomes_background_job(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def slow(self, args, limit, stdin=None, log_dir=None):
        await gate.wait()
        return EngineResult(args, 0, '[{"n": 1}]', "# 1 rows\n", 9.0)
    monkeypatch.setattr(EngineRunner, "_exec", slow)
    async with Client(build_server(Settings(workdir=tmp_path / "work", soft_timeout=1))) as c:
        err, res = await call(c, "devo_query", {"linq": "from t group by x select count()"})
        assert not err and res["state"] == "running" and res["job_id"].startswith("job-query-")
        gate.set()
        for _ in range(100):
            err, st = await call(c, "devo_job_status", {"job_id": res["job_id"]})
            if st["state"] != "running":
                break
            await asyncio.sleep(0.02)
    assert st["state"] == "done" and st["result"] == [{"n": 1}]


async def test_fast_call_returns_inline_without_leftovers(tmp_path, fake):
    fake.reply["query"] = (0, '[{"n": 1}]', "")
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_query", {"linq": "from t"})
    assert res["result"] == [{"n": 1}] and "job_id" not in res and os.listdir(tmp_path / "work") == []


@pytest.mark.parametrize("jobs", [
    [{"name": "x", "query_file": "/proc/self/environ"}],
    [{"name": "x", "query": "from t", "query_file": "../../etc/passwd"}],
    [{"name": "x", "query": "from t", "out": "/tmp/x"}],
    [{"name": "x", "query": "from t", "parallel": 200}],
])
async def test_batch_refuses_unsafe_inline_jobs(tmp_path, fake, jobs):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_batch", {"jobs": jobs})
    assert err and not fake.calls


async def test_batch_workspace_spec_is_checked_and_copied(tmp_path, fake):
    work = tmp_path / "work"
    async with Client(server(tmp_path)) as c:
        (work / "evil").mkdir(parents=True, exist_ok=True)
        (work / "evil" / "spec.json").write_text(json.dumps({"jobs": [{"name": "x", "query_file": "/etc/hostname"}]}))
        err, msg = await call(c, "devo_batch", {"spec": "evil/spec.json"})
        assert err and "query_file" in msg and not fake.calls
        (work / "ok").mkdir()
        (work / "ok" / "stage2.jsonl").write_text('# stage 2\n{"name": "a", "query": "from t", "parallel": 6}\n')
        err, res = await call(c, "devo_batch", {"spec": "ok/stage2.jsonl"})
    assert not err, res
    run_spec = fake.calls[0][-1]
    assert run_spec.startswith(str(work / res["dir"]))  # the checked copy, not the client's file
    assert json.loads(open(run_spec).read()) == {"name": "a", "query": "from t", "parallel": 6}


async def test_bundled_spec_runs_as_shipped(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_batch", {"spec": "credential-attacks", "vars": {"from": "7d"}})
    assert not err and fake.calls[0][-1] == str(references.bundled_specs()["credential-attacks"])


async def test_lag_extra_tables_are_validated(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_lag", {"extra_tables": ["a.b", "--out=/etc/x"]})
    assert err and not fake.calls


async def test_clearing_notes_needs_include_notes(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_cache", {"action": "clear", "args": ["notes"]})
        assert err and "include_notes" in msg and not fake.calls
        err, _ = await call(c, "devo_cache", {"action": "clear", "args": ["notes"], "include_notes": True})
    assert not err and fake.calls[0][-2:] == ["clear", "notes"]


async def test_nul_in_workspace_path_is_a_tool_error(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, msg = await call(c, "devo_workspace_read", {"path": "a\x00b"})
    assert err and "NUL" in msg


async def test_results_are_scrubbed_of_server_paths(tmp_path, fake):
    fake.reply["cache"] = (0, json.dumps({"dir": str(tmp_path / "work" / "x"), "engine": str(ENGINE)}), "")
    async with Client(server(tmp_path)) as c:
        err, res = await call(c, "devo_cache", {"action": "status"})
    assert not err and str(tmp_path) not in json.dumps(res) and str(ENGINE.parent) not in json.dumps(res)


async def test_no_empty_job_dirs_left_behind(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        err, _ = await call(c, "devo_tables", {})
    assert not err and os.listdir(tmp_path / "work") == []


async def test_killed_engine_keeps_its_output(tmp_path):
    from devo_mcp.workspace import Workspace
    script = tmp_path / "engine.sh"
    script.write_text("#!/bin/sh\necho partial-out\necho '# progress' >&2\nexec sleep 30\n")
    script.chmod(0o755)
    r = await EngineRunner(str(script), Workspace(tmp_path / "work")).run(["query"], 1)
    assert r.exit_code == 124 and "partial-out" in r.stdout and "# progress" in r.stderr
    assert "killed by the MCP server after 1s" in r.stderr


async def test_reference_grep_is_plain_terms(tmp_path, fake):
    async with Client(server(tmp_path)) as c:
        # a catastrophic regex is just text now: it returns at once instead of pinning a thread
        err, hit = await call(c, "devo_reference", {"name": "linq-traps", "grep": "(a+)+$" + "a" * 40})
        assert not err and hit["matched_lines"] == 0
        err, hit = await call(c, "devo_reference", {"name": "linq-traps", "grep": "WEAKHAS|nosuchword"})
    assert not err and hit["matched_lines"] > 0
