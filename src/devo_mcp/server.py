"""The Devo MCP server: tools, resources and prompts over the vendored Devo engine."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, references
from .cache_warmer import CacheWarmer
from .engine_runner import ENGINE_DIR, EXIT_MEANING, GRACE, EngineResult, EngineRunner, describe
from .guide import INSTRUCTIONS
from .settings import Settings, token_warning
from .workspace import Workspace, WorkspaceError

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CACHE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

CACHE_ENTRIES = {"tables", "fields", "event-time", "sources", "all"}
TABLE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]*")
JOB_FILES = ("status.json", "stdout.txt", "stderr.txt")
HEARTBEAT = 15  # seconds between progress notifications while the engine runs
CONFIRM_TTL = 1800  # seconds a write preview's confirmation token stays valid
MESSAGE_LINES = 80
MAX_PARALLEL = 8  # windows one query or batch job may run at once
BATCH_KEYS = {"name", "query", "from", "to", "chunk", "limit", "format", "auto_split", "timeout", "parallel",
              "min_split", "merge"}

# Shared parameter types
Start = Annotated[str, Field(description="Window start: 15m, 24h, 7d, now, today, yesterday, ISO date/time "
                                         "(UTC unless offset given), epoch, or a Devo now() expression")]
End = Annotated[str, Field(description="Window end (same forms as start)")]
Tz = Annotated[str | None, Field(description="IANA zone, e.g. Europe/London: adds *_local times next to UTC ones")]
Timeout = Annotated[int, Field(ge=10, le=3600, description="Seconds before the engine aborts")]
Background = Annotated[bool, Field(description="Return a job_id at once and run in the background; poll "
                                               "devo_job_status. Use for runs longer than a minute or two")]
Confirm = Annotated[bool, Field(description="send it. Only after the user approved the preview")]
Token = Annotated[str | None, Field(description="the confirmation_token from the preview")]


def checked_jobs(jobs: Any) -> list[dict]:
    """Batch jobs from a client: only the keys the engine runs (never query_file, which makes the
    engine read a file of the client's choosing), and per-job parallelism within the tool's limit."""
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
        raise ToolError("batch jobs must be a non-empty list of job objects")
    for i, j in enumerate(jobs, 1):
        if "query_file" in j:
            raise ToolError(f"job {i}: query_file is only for bundled specs; put the LINQ in `query`")
        bad = sorted(set(j) - BATCH_KEYS)
        if bad:
            raise ToolError(f"job {i}: unknown key(s) {', '.join(bad)}; allowed: {', '.join(sorted(BATCH_KEYS))}")
        p = j.get("parallel")
        if p is not None and (type(p) is not int or not 1 <= p <= MAX_PARALLEL):
            raise ToolError(f"job {i}: parallel must be a whole number from 1 to {MAX_PARALLEL}")
    return jobs


def engine_provenance() -> dict:
    try:
        return json.loads((ENGINE_DIR / "VENDORED.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    ws = Workspace(settings.workdir, settings.work_ttl_hours)
    runner = EngineRunner(settings.python, ws, settings.max_concurrency)
    confirm_key = secrets.token_bytes(32)
    warmer = CacheWarmer(runner, settings, "no Devo token" if token_warning() else None)

    mcp = MCPServer(
        name="devo",
        title="Devo SIEM",
        description="Query, investigate and triage in the Devo SIEM; build Activeboards",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    # ---------------------------------------------------------------- helpers

    async def wait_for_job(task, soft: int, ctx: Context | None, label: str) -> EngineResult | None:
        """The job's EngineResult if it finishes within `soft` seconds, else None (it keeps
        running). Sends progress notifications meanwhile. A cancelled call leaves the job running."""
        t0 = time.monotonic()
        while True:
            left = soft - (time.monotonic() - t0)
            if left <= 0:
                return None
            done, _ = await asyncio.wait({task}, timeout=min(HEARTBEAT, left))
            if done:
                r = task.result()
                if r is None:
                    raise ToolError(f"{label}: the engine job crashed; see devo_workspace_list for its dir")
                return r
            if ctx is not None:
                try:
                    secs = int(time.monotonic() - t0)
                    await ctx.report_progress(secs, None, f"{label} running for {secs}s")
                except Exception:
                    pass

    def tidy_job_dir(d: Path) -> None:
        """A finished interactive job: drop its bookkeeping (package() removes the dir if empty)."""
        for name in JOB_FILES:
            try:
                (d / name).unlink()
            except OSError:
                pass

    async def engine(args: list[str], timeout: int, ctx: Context | None = None, wall: int | None = None) -> EngineResult:
        """Run the engine. timeout is the engine's per-query limit; wall (default timeout + GRACE)
        is when the server kills the process."""
        task = asyncio.ensure_future(runner.run(args, wall or timeout + GRACE))
        t0 = time.monotonic()
        while True:
            try:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT)
            except asyncio.CancelledError:
                task.cancel()  # the client went away: the runner kills the engine process
                raise
            if done:
                return task.result()
            if ctx is not None:
                try:
                    secs = int(time.monotonic() - t0)
                    await ctx.report_progress(secs, None, f"{args[0]} running for {secs}s")
                except Exception:
                    pass  # progress is best effort: a client may not ask for it

    def messages(stderr: str) -> list[str]:
        lines = [scrub(ln) for ln in stderr.splitlines() if ln.strip()]
        if len(lines) > MESSAGE_LINES:
            lines = lines[:MESSAGE_LINES // 2] + [f"… {len(lines) - MESSAGE_LINES} lines omitted …"] + \
                lines[-MESSAGE_LINES // 2:]
        return lines

    root_prefix = str(ws.root) + "/"
    engine_prefix = str(ENGINE_DIR / "references") + "/"

    def scrub(text: str) -> str:
        """Server paths -> workspace-relative paths (what devo_workspace_read takes) and reference names."""
        return (text.replace(root_prefix, "").replace(engine_prefix, "references/")
                .replace(str(ENGINE_DIR), "<engine>").replace(str(ws.root), "."))

    def scrub_json(value: Any) -> Any:
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, list):
            return [scrub_json(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub_json(v) for k, v in value.items()}
        return value

    def fail(r: EngineResult) -> ToolError:
        meaning = EXIT_MEANING.get(r.exit_code, "error")
        tail = "\n".join(messages(r.stderr)[-30:]) or r.stdout[-3000:]
        return ToolError(f"Devo engine exit {r.exit_code} ({meaning}) for {scrub(describe(r.args))}\n{scrub(tail)}\n"
                         "Read the ERROR/hint lines above; for LINQ failures see devo_reference "
                         "name=linq-traps.md. Commands named as `devo.py <cmd>` map to the devo_<cmd> tools.")

    def fit(value: Any, budget: int, save_dir: Path | None, stem: str,
            saved: Path | None = None) -> tuple[Any, dict | None]:
        """Keep a result inside the response budget; save the whole of it to the workspace if not
        (or point at `saved`, a workspace file that already holds it)."""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= budget:
            return value, None
        full = saved
        if full is None:
            d = save_dir or ws.new_dir("output")
            full = d / (f"{stem}.txt" if isinstance(value, str) else f"{stem}.json")
            full.write_text(text if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1,
                                                                           default=str), encoding="utf-8")
        note = {"full_output": ws.rel(full), "full_chars": len(text),
                "hint": "result cut to fit the response; page through full_output with devo_workspace_read, "
                        "or aggregate on the server (group by … select count())"}
        if isinstance(value, list):
            lo, hi = 0, len(value)
            while lo < hi:  # the longest prefix that fits
                mid = (lo + hi + 1) // 2
                if len(json.dumps(value[:mid], ensure_ascii=False, default=str)) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            note.update(items_shown=lo, items_total=len(value))
            return value[:lo], note
        if isinstance(value, dict):
            return text[:budget], note | {"note": "JSON object cut mid-way: read full_output"}
        return value[:budget], note

    def package(r: EngineResult, parse_json: bool = False, out_dir: Path | None = None,
                stem: str = "output", **extra) -> dict:
        """Turn an engine run into a tool result: output, the engine's messages, and files."""
        if not r.ok:
            raise fail(r)
        res: dict[str, Any] = {"ok": True, "command": scrub(describe(r.args)), "seconds": r.seconds}
        out: Any = scrub(r.stdout)
        if parse_json and r.stdout.strip():
            try:
                out = scrub_json(json.loads(r.stdout))
            except json.JSONDecodeError:
                pass
        budget = settings.max_output_chars - sum(len(str(v)) for v in extra.values())
        res["result"], note = fit(out, max(2000, budget), out_dir, stem)
        if note:
            res["truncated"] = note
        res["messages"] = messages(r.stderr)
        if out_dir is not None and out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()  # nothing was written (e.g. an inline query)
        elif out_dir is not None:
            res["dir"] = ws.rel(out_dir)
            res["files"] = [ws.rel(p) for p in sorted(out_dir.rglob("*")) if p.is_file()][:200]
        res.update({k: v for k, v in extra.items() if v is not None})
        return res

    def attach(res: dict, path: Path, key: str, budget: int | None = None) -> dict:
        """Add a JSON/text report the engine wrote to the result (cut to fit)."""
        if not path.is_file():
            return res
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            value: Any = scrub_json(json.loads(raw))
        except json.JSONDecodeError:
            value = scrub(raw)
        room = budget or max(2000, settings.max_output_chars - len(json.dumps(res, default=str)))
        res[key], note = fit(value, room, None, "", saved=path)
        if note:
            res.setdefault("truncated_" + key, note)
        return res

    def opt(args: list[str], flag: str, value) -> None:
        if value is None or value is False or value == "":
            return
        if value is True:
            args.append(flag)
        elif isinstance(value, (list, tuple)):
            args.append(f"{flag}={','.join(str(v) for v in value)}")
        else:
            args.append(f"{flag}={value}")  # one token: a value starting with "-" can't parse as an option

    def table_name(t: str) -> str:
        t = t.strip()
        if not TABLE_NAME.fullmatch(t):
            raise ToolError(f"{t!r} is not a table name (letters, digits, _ and . only)")
        return t

    def board_ref(b: str, allow_default: bool = False) -> str:
        b = b.strip()
        if not (re.fullmatch(r"\d+", b) or (allow_default and b == "default")):
            raise ToolError(f"board id {b!r} must be numeric (from devo_boards)" + (" or 'default'" if allow_default else ""))
        return b

    def ws_path(rel: str, must_exist: bool = True) -> Path:
        try:
            return ws.resolve(rel, must_exist)
        except WorkspaceError as e:
            raise ToolError(str(e))

    def read_spec(path: Path) -> list:
        """A workspace batch spec, parsed the way the engine parses it (JSON, {jobs: […]} or JSON Lines)."""
        if not path.is_file():
            raise ToolError(f"{ws.rel(path)!r} is not a spec file")
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            obj = json.loads(text)
            return (obj["jobs"] if "jobs" in obj else [obj]) if isinstance(obj, dict) else obj
        except ValueError:
            try:
                return [json.loads(ln) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
            except ValueError as e:
                raise ToolError(f"{ws.rel(path)}: not JSON or JSON Lines ({e})")

    async def run_or_job(kind: str, build_args, timeout: int, background: bool, ctx: Context | None,
                         finish) -> dict:
        """Run as a job and wait for it, up to the soft timeout; past that, hand back the job_id
        and let it finish in the background (MCP clients commonly give up on a call after ~60s).
        background=True returns the job_id at once. build_args(dir) -> args; finish(result, dir)
        -> the tool result."""
        limit = max(timeout + GRACE, settings.max_job_seconds)
        if background:
            st = runner.start_job(kind, build_args, limit)
            return st | {"command": scrub(st["command"]),
                         "hint": "poll devo_job_status with this job_id; outputs land in dir"}
        if settings.soft_timeout <= 0:  # wait as long as it takes
            d = ws.new_dir(kind)
            r = await engine(build_args(d), timeout, ctx, wall=max(timeout + GRACE, settings.max_call_seconds))
            return finish(r, d)
        # The job pool, not the call pool: a call handed off here may run for hours, and must not hold
        # a slot the quick calls (devo_check, devo_schema, …) wait on.
        st = runner.start_job(kind, build_args, limit)
        task, d = runner.job_task(st["job_id"]), ws.resolve(st["dir"])
        r = await wait_for_job(task, settings.soft_timeout, ctx, kind)
        if r is None:
            return st | {"command": scrub(st["command"]), "state": "running",
                         "still_running_after_seconds": settings.soft_timeout,
                         "hint": "This is taking longer than one tool call should, so it carries on as a "
                                 "background job. Poll devo_job_status with this job_id (every 20-30s) for the "
                                 "result; or narrow the window / aggregate more next time."}
        tidy_job_dir(d)  # finish() lists and attaches the tool's own files only
        res = finish(r, d)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()  # the tool wrote nothing there
        return res

    # ---------------------------------------------------------------- confirmation tokens (writes)

    spent: dict[str, float] = {}  # nonce -> when it stops mattering: each token confirms one change

    def token_mac(action: str, params: dict, nonce: str, bucket: int) -> str:
        msg = json.dumps([action, params, nonce, bucket], sort_keys=True, default=str).encode()
        return hmac.new(confirm_key, msg, hashlib.sha256).hexdigest()[:24]

    def confirmation_token(action: str, params: dict) -> str:
        nonce = secrets.token_hex(6)
        return f"{nonce}.{token_mac(action, params, nonce, int(time.time() // CONFIRM_TTL))}"

    def check_confirmation(action: str, params: dict, token: str | None) -> None:
        if not token:
            raise ToolError("confirm=true needs the confirmation_token from a preview of this exact change. "
                            "Call without confirm first, show the preview to the user, and confirm only "
                            "after they approve it.")
        now = time.time()
        for k in [k for k, until in spent.items() if until < now]:
            del spent[k]
        nonce, _, mac = token.strip().partition(".")
        bucket = int(now // CONFIRM_TTL)
        if nonce in spent:
            raise ToolError("this confirmation_token was already used: each preview approves one change. "
                            "Preview again and get the user's approval for what it shows.")
        for b in (bucket, bucket - 1):
            if hmac.compare_digest(mac.encode(), token_mac(action, params, nonce, b).encode()):
                spent[nonce] = now + 2 * CONFIRM_TTL
                return
        raise ToolError("confirmation_token doesn't match this change (different parameters, expired, or the "
                        "server restarted). Preview again and get the user's approval for what it shows.")

    async def write_action(action: str, params: dict, args: list[str], confirm: bool,
                           confirmation: str | None, ctx: Context | None, timeout: int = 120) -> dict:
        if confirm:
            check_confirmation(action, params, confirmation)
            i = args.index("--")  # options before the positional separator, or argparse rejects it
            r = await engine(args[:i] + ["--confirm"] + args[i:], timeout, ctx)
            return package(r) | {"sent": True}
        r = await engine(args, timeout, ctx)
        return package(r) | {"sent": False, "confirmation_token": confirmation_token(action, params),
                             "next": "Show this preview to the user. Only after they explicitly approve it, call "
                                     "again with the same parameters, confirm=true and this confirmation_token."}

    # ---------------------------------------------------------------- session and discovery

    @mcp.tool(annotations=READ)
    async def devo_check(ctx: Context) -> dict:
        """Verify the Devo token and region and summarise the local domain cache. Start a session with
        this. If the token belongs to another region it says which DEVO_REGION to set."""
        res = package(await engine(["check"], 120, ctx))
        return res | {"server": {"version": __version__, "writes_enabled": settings.allow_writes,
                                 "engine": engine_provenance(), "cache_warmup": warmer.describe()}}

    @mcp.tool(annotations=READ)
    async def devo_tables(grep: Annotated[str | None, Field(description="regex on table name, e.g. 'win|azure'")] = None,
                          live: Annotated[bool, Field(description="refetch from Devo now and refresh the cache")] = False,
                          ctx: Context = None) -> dict:
        """Tables that have data in this domain (last 7 days), with volumes. Cached; live=true refetches."""
        args = ["tables"]
        opt(args, "--grep", grep)
        opt(args, "--live", live)
        return await run_or_job("tables", lambda d: args, 300, False, ctx, lambda r, d: package(r))

    @mcp.tool(annotations=READ)
    async def devo_schema(table: Annotated[str, Field(description="exact table name, e.g. box.win_nxlog.security")],
                          grep: Annotated[str | None, Field(description="regex on field name")] = None,
                          ctx: Context = None) -> dict:
        """A table's complete field list with types, live from Devo. Field names are case-sensitive."""
        args = ["schema", "--format", "json"]
        opt(args, "--grep", grep)
        args += ["--", table_name(table)]
        return package(await engine(args, 120, ctx), parse_json=True)

    @mcp.tool(annotations=READ)
    async def devo_fields(
            table: Annotated[str | None, Field(description="table (exact or substring); omit to search by role")] = None,
            role: Annotated[Literal["user", "host", "ip", "time", "join-id", "hash", "url/domain", "process/file",
                                    "action", "outcome", "text", "constant"] | None,
                            Field(description="without table: which tables/fields hold this kind of value; with "
                                              "table: only those fields")] = None,
            grep: Annotated[str | None, Field(description="with table: field regex; with role: table regex")] = None,
            min_fill: Annotated[float, Field(ge=0, le=1)] = 0.05,
            include_empty: bool = False,
            refresh: Annotated[bool, Field(description="re-profile the table live now")] = False,
            ctx: Context = None) -> dict:
        """Field profile of a table: populated fields, type, fill rate, value pattern, case and role
        (shapes only, never values). Profiled live on first use, then cached. Or, with only role,
        every profiled table/field holding IPs, users, hosts, join ids…"""
        args = ["fields"]
        opt(args, "--role", role)
        opt(args, "--grep", grep)
        args += [f"--min-fill={min_fill}"]
        opt(args, "--all", include_empty)
        opt(args, "--refresh", refresh)
        if table:
            args += ["--", table_name(table)]
        return package(await engine(args, 300, ctx))

    @mcp.tool(annotations=READ)
    async def devo_profile(tables: Annotated[list[str], Field(min_length=1, max_length=50)],
                           sample: Annotated[int, Field(ge=20, le=5000)] = 300,
                           include_empty: bool = False, ctx: Context = None) -> dict:
        """Profile tables from live samples now (fields, fill, patterns, roles; no values) and store the
        profiles in the domain cache."""
        args = ["profile", f"--sample={sample}", "--format", "json"]
        opt(args, "--all", include_empty)
        args += ["--", *map(table_name, tables)]
        return await run_or_job("profile", lambda d: args, 600, False, ctx, lambda r, d: package(r, parse_json=True))

    # ---------------------------------------------------------------- queries

    @mcp.tool(annotations=READ)
    async def devo_query(
            linq: Annotated[str, Field(description="LINQ: from <table> where … select … as … group [every 1h] by … "
                                                   "select <aggregations>")],
            start: Start = "1h", end: End = "now",
            limit: Annotated[int, Field(ge=0, description="max rows (0 = no limit: careful). Inline results are "
                                                          "also cut to the response budget")] = 200,
            format: Annotated[Literal["json", "csv", "table"],
                              Field(description="inline result format; csv is the most compact")] = "json",
            sort: Annotated[str | None, Field(description="client-side sort: field or -field, comma-separated "
                                                          "(LINQ has no ORDER BY)")] = None,
            chunk: Annotated[str | None, Field(description="split the range into windows (e.g. 6h, 1d) run in "
                                                           "parallel: for day-plus ranges on busy tables")] = None,
            parallel: Annotated[int, Field(ge=1, le=8)] = 4,
            auto_split: Annotated[bool | None, Field(description="halve windows that hit the limit and re-run "
                                                                 "them (default: on for raw rows with limit>=1000)")] = None,
            stats: Annotated[bool, Field(description="return per-column fill/distinct/top values instead of rows")] = False,
            head: Annotated[int | None, Field(ge=1, description="return only the first N rows")] = None,
            tz: Tz = None,
            ui_safe: Annotated[bool, Field(description="warn about identifiers the Devo web UI rejects")] = False,
            save: Annotated[bool, Field(description="write all rows to a workspace .jsonl file and return column "
                                                    "stats instead of rows; page the file with devo_workspace_read")] = False,
            timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Run a LINQ query over a time range. Messages report the resolved UTC range, row count and
        whether the limit truncated the result: quote them. Aggregate on the server before pulling
        raw rows; run narrow first, then widen. Read devo_reference linq-traps.md when a query
        fails or returns zero rows."""
        def build(d: Path) -> list[str]:
            args = ["query", f"--from={start}", f"--to={end}", f"--limit={limit}",
                    f"--timeout={timeout}", f"--parallel={parallel}"]
            opt(args, "--sort", sort)
            opt(args, "--chunk", chunk)
            if auto_split is not None:
                args.append("--auto-split" if auto_split else "--no-auto-split")
            opt(args, "--tz", tz)
            opt(args, "--ui-safe", ui_safe)
            if save or background:
                args += ["--format", "jsonl", f"--out={d / 'rows.jsonl'}"]
                opt(args, "--head", head)
            else:
                args += [f"--format={format}", "--width", "0"]
                opt(args, "--stats", stats)
                opt(args, "--head", head)
            return args + ["--", linq]

        def finish(r: EngineResult, d: Path) -> dict:
            return package(r, parse_json=(format == "json" and not save), out_dir=d, stem="rows")

        return await run_or_job("query", build, timeout, background, ctx, finish)

    @mcp.tool(annotations=READ)
    async def devo_batch(
            spec: Annotated[str | None, Field(description="a bundled spec name (see devo_reference name=specs) such "
                                                          "as credential-attacks, privileged-grants, file-malware, or "
                                                          "a workspace path to a .jsonl spec (e.g. a creds stage2)")] = None,
            jobs: Annotated[list[dict[str, Any]] | None,
                            Field(description="inline jobs instead of spec: [{name, query, from?, to?, chunk?, limit?, "
                                              "format?, auto_split?, timeout?}]")] = None,
            vars: Annotated[dict[str, str] | None,
                            Field(description="values for {name} placeholders in the jobs, e.g. {user: jsmith, from: 7d}")] = None,
            start: Annotated[str, Field(description="default start for jobs without one")] = "1h",
            end: Annotated[str, Field(description="default end")] = "now",
            limit: Annotated[int, Field(ge=0)] = 10000,
            parallel: Annotated[int, Field(ge=1, le=8)] = 4,
            tz: Tz = None, timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Run many queries in parallel from a spec; each job writes <name>.jsonl and <name>.log in the
        output dir, plus batch-summary.json. Use for sweeps and the bundled playbook specs instead of
        calling devo_query in a loop. Read the job files with devo_workspace_read."""
        if bool(spec) == bool(jobs):
            raise ToolError("give exactly one of spec or jobs")
        # Bundled specs are trusted (their jobs use query_file). Anything a client supplies, inline or
        # as a workspace file, is checked and rewritten into the job dir, so the engine never sees a
        # query_file of the client's choosing.
        spec_path = references.bundled_specs().get(spec.removesuffix(".jsonl")) if spec else None
        client_jobs = None if spec_path else checked_jobs(jobs if jobs else read_spec(ws_path(spec)))

        def build(d: Path) -> list[str]:
            path = spec_path
            if client_jobs is not None:
                path = d / "spec.jsonl"
                path.write_text("\n".join(json.dumps(j) for j in client_jobs) + "\n", encoding="utf-8")
            out = d / "out"
            out.mkdir()
            args = ["batch", f"--out-dir={out}", f"--from={start}", f"--to={end}",
                    f"--limit={limit}", f"--parallel={parallel}", f"--timeout={timeout}"]
            if vars:
                for k, v in vars.items():
                    if not re.fullmatch(r"[A-Za-z_]\w*", k) or "," in str(v):
                        raise ToolError(f"vars: bad name {k!r} or a comma in its value (the engine splits on commas)")
                    args.append(f"--vars={k}={v}")
            opt(args, "--tz", tz)
            return args + ["--", str(path)]

        def finish(r: EngineResult, d: Path) -> dict:
            return attach(package(r, out_dir=d), d / "out" / "batch-summary.json", "summary")

        return await run_or_job("batch", build, timeout, background, ctx, finish)

    # ---------------------------------------------------------------- alerts

    @mcp.tool(annotations=READ)
    async def devo_alerts(
            start: Start = "24h", end: End = "now",
            open_only: Annotated[bool, Field(description="hide Closed (300) and False positive (2)")] = False,
            name: Annotated[str | None, Field(description="only definitions whose name contains this")] = None,
            by_def: Annotated[bool, Field(description="one row per definition: counts per status, source table, "
                                                      "native/forwarded: the quickest way to choose what to work")] = False,
            group_by: Annotated[str | None, Field(description="group alerts into incidents by extra-data values: "
                                                              "file, site, user, host, hash, ip (comma-separated) or "
                                                              "a key regex")] = None,
            group_by_parent: bool = False,
            limit: Annotated[int, Field(ge=1, le=1000)] = 50,
            max_fetch: Annotated[int, Field(ge=1, le=10000)] = 1000,
            raw: Annotated[bool, Field(description="the raw API objects plus extraDataDecoded (large: ~5k chars "
                                                   "per alert) instead of compact CSV rows")] = False,
            ctx: Context = None) -> dict:
        """Triggered alerts, newest first, with status, priority, source table, event time from the
        evidence, and mapped user (compact CSV; devo_alert for one alert in full). Status codes: 0 Unread, 1 Updated, 2 False positive, 100 Watched,
        300 Closed, 800 Suppressed; others are custom workflow states. Priority 1-10 (6+ high)."""
        args = ["alerts", f"--from={start}", f"--to={end}", f"--limit={limit}", f"--max={max_fetch}"]
        opt(args, "--open", open_only)
        opt(args, "--name", name)
        opt(args, "--by-def", by_def)
        opt(args, "--group-by", group_by)
        opt(args, "--group-by-parent", group_by_parent)
        if not group_by:
            args += ["--format", "json" if (raw or by_def) else "csv"]
        return await run_or_job("alerts", lambda d: args, 300, False, ctx,
                                lambda r, d: package(r, parse_json=raw or by_def))

    @mcp.tool(annotations=READ)
    async def devo_alert(alert_id: Annotated[str, Field(description="numeric alert id")],
                         raw: Annotated[bool, Field(description="the raw API objects (plus extraDataDecoded) instead "
                                                               "of the readable report")] = False,
                         full: Annotated[bool, Field(description="don't truncate long evidence values")] = False,
                         ctx: Context = None) -> dict:
        """One triggered alert: definition and description, source table, the detection's LINQ query,
        decoded evidence (extra data), raw event time vs alert time, and analyst comments."""
        if not re.fullmatch(r"\d+", alert_id.strip()):
            raise ToolError("alert_id must be numeric")
        args = ["alert"]
        opt(args, "--json", raw)
        opt(args, "--full", full)
        args += ["--", alert_id.strip()]
        return package(await engine(args, 120, ctx), parse_json=raw)

    @mcp.tool(annotations=READ)
    async def devo_alert_definitions(
            name: Annotated[str | None, Field(description="name filter (server-side)")] = None,
            include_query: Annotated[bool, Field(description="include each definition's LINQ")] = False,
            page: Annotated[int, Field(ge=0)] = 0, size: Annotated[int, Field(ge=1, le=200)] = 20,
            ctx: Context = None) -> dict:
        """Alert definitions (detection rules) in this domain."""
        args = ["alert-defs", f"--page={page}", f"--size={size}", "--format", "json"]
        opt(args, "--name", name)
        opt(args, "--query", include_query)
        return package(await engine(args, 120, ctx), parse_json=True)

    # ---------------------------------------------------------------- investigations

    @mcp.tool(annotations=READ)
    async def devo_coverage(
            name: Annotated[str, Field(description="distinctive part of the host name (case-insensitive)")],
            start: Annotated[str, Field(description="Windows window start")] = "30d", end: End = "now",
            linux_start: Annotated[str, Field(description="Linux and Zscaler window start (slow table)")] = "7d",
            windows_only: Annotated[bool, Field(description="skip Linux and Zscaler: a quick answer")] = False,
            low_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.1,
            timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Is a host logging? Daily Windows/Linux event counts, gaps and low days, agent restarts, and
        Zscaler ZPA sessions. Built on box.win_nxlog.*, box.unix and vpn.zscaler.access: sources this
        domain doesn't have are skipped. Run this before concluding a host 'did nothing'."""
        def build(d: Path) -> list[str]:
            args = ["coverage", f"--from={start}", f"--to={end}", f"--linux-from={linux_start}",
                    f"--low-ratio={low_ratio}", "--format", "json", f"--timeout={timeout}"]
            opt(args, "--no-linux", windows_only)
            return args + ["--", name]
        return await run_or_job("coverage", build, timeout, background, ctx,
                                lambda r, d: package(r, parse_json=True, out_dir=d))

    @mcp.tool(annotations=READ)
    async def devo_activity(
            terms: Annotated[list[str], Field(min_length=1, description="by=user: UPN, surname or account names "
                                                                        "(OR'd); by=ip: IPs; by=host: the shortest "
                                                                        "distinctive part of host names")],
            by: Literal["user", "ip", "host"] = "user",
            start: Start = "today", end: End = "now",
            expand: Annotated[bool, Field(description="from a UPN first.last@domain also search the domain's "
                                                      "account-name forms (devo_cache naming)")] = False,
            rows: Annotated[bool, Field(description="raw rows instead of grouped counts (for devo_timeline "
                                                    "per-event detail; slower and bigger)")] = False,
            no_auto: Annotated[bool, Field(description="skip sources generated from field profiles: faster, "
                                                       "curated sources only")] = True,
            graph_ids: Annotated[list[str] | None, Field(description="Entra object ids for the Microsoft Graph "
                                                                     "activity pass")] = None,
            graph_only_into: Annotated[str | None, Field(description="run only the Graph pass and add it to this "
                                                                     "earlier activity dir")] = None,
            only: Annotated[list[str] | None, Field(description="source names to run")] = None,
            skip: Annotated[list[str] | None, Field(description="source names to skip")] = None,
            limit: Annotated[int, Field(ge=1, le=100000, description="max rows per source per chunk")] = 5000,
            tz: Tz = None, timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Everything a user, IP or host did: a parallel sweep over every validated source in this
        domain, one .jsonl per source plus summary.json in the returned dir. Follow with
        devo_timeline on that dir. A source flagged FEED GAP has no data after that point (not 'no
        activity')."""
        def build(d: Path) -> list[str]:
            target = ws_path(graph_only_into) if graph_only_into else d
            args = ["activity", f"--by={by}", f"--from={start}", f"--to={end}", f"--out-dir={target}",
                    f"--limit={limit}", f"--timeout={timeout}", "--width", "60"]
            for t in terms:
                args += [f"--term={t}"]
            opt(args, "--expand", expand)
            opt(args, "--rows", rows)
            opt(args, "--no-auto", no_auto)
            opt(args, "--graph-ids", graph_ids)
            opt(args, "--graph-only", bool(graph_only_into))
            opt(args, "--only", only)
            opt(args, "--skip", skip)
            opt(args, "--tz", tz)
            return args

        def finish(r: EngineResult, d: Path) -> dict:
            target = ws_path(graph_only_into) if graph_only_into else d
            return package(r, out_dir=target) | {"next": f"devo_timeline dir={ws.rel(target)}"}

        return await run_or_job("activity", build, timeout, background, ctx, finish)

    @mcp.tool(annotations=LOCAL)
    async def devo_timeline(dir: Annotated[str, Field(description="a devo_activity output dir (workspace path)")],
                            tz: Tz = None,
                            keep_outside: Annotated[bool, Field(description="keep back-filled events whose event "
                                                                            "time precedes the sweep window")] = False,
                            ctx: Context = None) -> dict:
        """Merge an activity sweep into one sorted, de-duplicated event list (event time, actor, action,
        target, ip, host) and a per-day summary. Work from the direct events (the account is the
        actor) and check the target, automated and raw-text ones. Offline: no Devo calls."""
        d = ws_path(dir)
        if (d / "summary.json").is_file() is False:
            raise ToolError(f"{dir!r} has no summary.json: pass the dir returned by devo_activity")
        args = ["timeline", f"--out={d / 'timeline.json'}"]
        opt(args, "--tz", tz)
        opt(args, "--keep-outside", keep_outside)
        args += ["--", str(d)]
        res = package(await engine(args, 300, ctx), out_dir=d)
        return attach(res, d / "timeline.txt", "day_summary")

    @mcp.tool(annotations=READ)
    async def devo_teams(subject: Annotated[str, Field(description="the user's UPN (or a distinctive part), or a "
                                                                   "thread id 19:…")],
                         start: Start = "today", end: End = "now", tz: Tz = None,
                         all_matches: Annotated[bool, Field(description="keep every account the term matches")] = False,
                         top: Annotated[int, Field(ge=1, le=200)] = 15,
                         timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Teams reconstruction: conversations (1:1, group, meeting, channel) with the other parties,
        messages sent, edits, files and links; meetings and calls per occurrence with join/leave;
        daily counts. Times are event times, never eventdate."""
        def build(d: Path) -> list[str]:
            args = ["teams", f"--from={start}", f"--to={end}", f"--out={d / 'teams.json'}",
                    f"--top={top}", f"--timeout={timeout}"]
            opt(args, "--tz", tz)
            opt(args, "--all-matches", all_matches)
            return args + ["--", subject]
        return await run_or_job("teams", build, timeout, background, ctx,
                                lambda r, d: attach(package(r, out_dir=d), d / "teams.json", "teams"))

    @mcp.tool(annotations=READ)
    async def devo_logons(
            start: Start = "7d", end: End = "now",
            os: Annotated[Literal["windows,linux", "windows", "linux"], Field(description="which systems")] = "windows,linux",
            host: Annotated[str | None, Field(description="only servers whose name contains this")] = None,
            user: Annotated[str | None, Field(description="only accounts containing this")] = None,
            people_only: bool = False,
            resolve_ztna: Annotated[bool, Field(description="also list who reached each server over RDP/SSH "
                                                            "through Zscaler ZPA (needs vpn.zscaler.access)")] = False,
            include_network: Annotated[bool, Field(description="also count network logons (type 3): much more "
                                                               "volume")] = False,
            all_accounts: Annotated[bool, Field(description="also built-in/computer accounts")] = False,
            tz: Tz = None, limit: Annotated[int, Field(ge=1, le=1000)] = 40,
            timeout: Timeout = 600, background: Background = False, ctx: Context = None) -> dict:
        """Who logged into which Windows/Linux servers: per server and account, first/last seen,
        sources, and a person/service label. The full report (every pair) is report.json and the
        analyst CSV logons.csv in the returned dir."""
        def build(d: Path) -> list[str]:
            args = ["logons", f"--from={start}", f"--to={end}", f"--os={os}", f"--limit={limit}",
                    f"--out={d / 'report.json'}", f"--csv={d / 'logons.csv'}", f"--timeout={timeout}"]
            opt(args, "--host", host)
            opt(args, "--user", user)
            opt(args, "--people-only", people_only)
            opt(args, "--resolve-ztna", resolve_ztna)
            opt(args, "--include-network", include_network)
            opt(args, "--all-accounts", all_accounts)
            opt(args, "--tz", tz)
            return args
        return await run_or_job("logons", build, timeout, background, ctx,
                                lambda r, d: package(r, out_dir=d))

    @mcp.tool(annotations=READ)
    async def devo_creds(
            dir: Annotated[str, Field(description="the out dir of devo_batch spec=credential-attacks "
                                                  "(<batch dir>/out)")],
            exclude: Annotated[list[str] | None, Field(description="IPs to ignore: your own egress/ZTNA/NAT")] = None,
            stage2_dir: Annotated[str | None, Field(description="interpret a stage-2 batch output dir and write "
                                                                "stage3.jsonl")] = None,
            min_users: Annotated[int, Field(ge=1)] = 5, min_ips: Annotated[int, Field(ge=1)] = 5,
            window: str = "7d", ctx: Context = None) -> dict:
        """Summarise a credential-attack batch (password spray, distributed brute force) and write the
        stage-2 'did anything succeed' spec (<dir>/stage2.jsonl), to run with devo_batch spec=<that
        path>; then call again with stage2_dir. Offline: no Devo calls."""
        d = ws_path(dir)
        args = ["creds", f"--min-users={min_users}", f"--min-ips={min_ips}", f"--window={window}"]
        opt(args, "--exclude", exclude)
        if stage2_dir:
            args.append(f"--stage2-dir={ws_path(stage2_dir)}")
        res = package(await engine(args + ["--", str(d)], 300, ctx))
        for name in ("stage2.jsonl", "stage3.jsonl"):
            if (d / name).is_file():
                res[name.split(".")[0] + "_spec"] = ws.rel(d / name)
        if stage2_dir and (ws_path(stage2_dir) / "stage3.jsonl").is_file():
            res["stage3_spec"] = ws.rel(ws_path(stage2_dir) / "stage3.jsonl")
        return res

    @mcp.tool(annotations=LOCAL)
    async def devo_grants(dir: Annotated[str, Field(description="the out dir of devo_batch spec=privileged-grants")],
                          limit: Annotated[int, Field(ge=1, le=500)] = 25, ctx: Context = None) -> dict:
        """Summarise a privileged-grants batch: Entra role changes classified PIM / direct / permanent,
        and AD privileged-group changes. Offline: no Devo calls."""
        return package(await engine(["grants", f"--limit={limit}", "--", str(ws_path(dir))], 300, ctx))

    # ---------------------------------------------------------------- health

    @mcp.tool(annotations=READ)
    async def devo_lag(tables: Annotated[str | None, Field(description="regex: only default lag tables matching "
                                                                       "it, e.g. 'office365|win_nxlog'")] = None,
                       extra_tables: Annotated[list[str] | None, Field(description="tables outside the default "
                                                                                   "set")] = None,
                       start: Start = "24h", end: End = "now",
                       hourly: Annotated[bool, Field(description="include each table's per-hour rows")] = False,
                       tz: Tz = None, timeout: Timeout = 600, background: Background = False,
                       ctx: Context = None) -> dict:
        """Ingestion lag per table (eventdate minus the record's own event time), measured now. Run
        before trusting a recent window: Microsoft 365 and others back-fill."""
        def build(d: Path) -> list[str]:
            args = ["lag", f"--from={start}", f"--to={end}", f"--out={d / 'lag.json'}", f"--timeout={timeout}"]
            opt(args, "--tables", tables)
            for t in extra_tables or []:
                args += [f"--table={table_name(t)}"]
            opt(args, "--hourly", hourly)
            opt(args, "--tz", tz)
            return args
        return await run_or_job("lag", build, timeout, background, ctx, lambda r, d: package(r, out_dir=d))

    @mcp.tool(annotations=READ)
    async def devo_health(baseline: str = "21d", recent: str = "7d",
                          skip_hosts: Annotated[bool, Field(description="skip the Windows/Linux sender check "
                                                                        "(the slow part)")] = False,
                          skip_lag: bool = False,
                          limit: Annotated[int, Field(ge=1, le=500)] = 25,
                          timeout: Timeout = 900,
                          background: Annotated[bool, Field(description="recommended: this takes minutes")] = True,
                          ctx: Context = None) -> dict:
        """Health of the whole domain: log sources stopped, dropped or new against a baseline, quiet
        senders, collector errors and ingestion lag. Full report: health.json in the dir."""
        def build(d: Path) -> list[str]:
            args = ["health", f"--baseline={baseline}", f"--recent={recent}", f"--limit={limit}",
                    f"--out={d / 'health.json'}", f"--timeout={timeout}"]
            opt(args, "--no-hosts", skip_hosts)
            opt(args, "--no-lag", skip_lag)
            return args
        return await run_or_job("health", build, timeout, background, ctx, lambda r, d: package(r, out_dir=d))

    # ---------------------------------------------------------------- domain cache

    @mcp.tool(annotations=CACHE)
    async def devo_cache(
            action: Literal["status", "notes", "build", "verify", "refresh", "clear", "note_add", "note_verify",
                            "note_rm", "naming_show", "naming_set", "naming_reset", "service_show", "service_set",
                            "service_add", "service_reset"],
            args: Annotated[list[str] | None,
                            Field(description="refresh: tables | fields [TABLE…] | event-time | sources | all; "
                                              "clear: tables | fields | TABLE | event-time | sources | all; "
                                              "note_add: [fact text]; note_verify/note_rm: [ids]; naming_set: "
                                              "templates like {first}.{last}; service_set/add: name globs")] = None,
            profile: Annotated[str, Field(description="build: all, none, or N busiest tables")] = "all",
            include_notes: Annotated[bool, Field(description="clear: drop notes and naming too (only when the user "
                                                             "asks)")] = False,
            background: Annotated[bool, Field(description="build/verify take minutes: run them in the "
                                                          "background")] = False,
            ctx: Context = None) -> dict:
        """The domain cache (tables, schemas, field profiles, verified event time, sweep sources) and
        the domain notes: facts verified in this domain (account naming, custom alert statuses,
        known gaps). Notes hold facts only, never query results or personal data."""
        verb, _, sub = action.partition("_")
        if verb == "build" and warmer.running_job:
            st, _ = runner.job_status(warmer.running_job)
            return st | {"note": "the server's cache warm-up is already building the cache: poll this job with "
                                 "devo_job_status instead of starting a second build"}
        words = [str(w).strip() for w in (args or []) if str(w).strip()]
        if verb == "refresh" and words:
            if words[0] not in CACHE_ENTRIES or (words[0] != "fields" and len(words) > 1):
                raise ToolError("refresh takes one of tables, fields [TABLE…], event-time, sources, all")
            words = words[:1] + [table_name(w) for w in words[1:]]
        elif verb == "clear":
            if "notes" in words and not include_notes:
                raise ToolError("clearing the domain notes needs include_notes=true (only when the user asks)")
            words = [w if w in CACHE_ENTRIES | {"notes"} else table_name(w) for w in words]
        elif action == "note_add":
            if not words:
                raise ToolError("note_add needs the fact as args: [\"<fact, with the table/field and how it was checked>\"]")
            words = [" ".join(words)]
        elif verb in ("status", "notes", "build", "verify") and words:
            raise ToolError(f"{action} takes no args")
        cmd = ["cache"]
        if verb in ("status", "notes"):
            cmd.append("--json")
        if verb == "build":
            cmd.append(f"--profile={profile}")
        opt(cmd, "--include-notes", include_notes)
        cmd += ["--", verb] + ([sub] if sub else []) + words
        timeout = 3000 if verb in ("build", "verify", "refresh") else 120
        return await run_or_job("cache", lambda d: cmd, timeout, background, ctx,
                                lambda r, d: package(r, parse_json=verb in ("status", "notes")))

    # ---------------------------------------------------------------- dashboards (read / build / test)

    @mcp.tool(annotations=READ)
    async def devo_boards(grep: Annotated[str | None, Field(description="regex on name, description, tags")] = None,
                          ctx: Context = None) -> dict:
        """Activeboards (dashboards): id, last update, flags, name, tags, owner. Board ids aren't shown
        in the Devo UI, so start here."""
        args = ["boards", "--format", "json"]
        opt(args, "--grep", grep)
        return package(await engine(args, 120, ctx), parse_json=True)

    @mcp.tool(annotations=READ)
    async def devo_board(board_id: Annotated[str, Field(description="numeric id from devo_boards, or 'default'")],
                         raw: Annotated[bool, Field(description="the raw API object")] = False,
                         export: Annotated[bool, Field(description="also write {name, description, settings} to the "
                                                                   "workspace, for editing, devo_board_check and "
                                                                   "devo_board_push")] = False,
                         ctx: Context = None) -> dict:
        """One Activeboard: widgets, types, layout and each widget's LINQ."""
        args = ["board"]
        opt(args, "--json", raw)
        d = None
        if export:
            d = ws.new_dir("board")
            args += [f"--out={d / 'board.json'}"]
        args += ["--", board_ref(board_id, allow_default=True)]
        res = package(await engine(args, 120, ctx), parse_json=raw, out_dir=d)
        if d is not None:
            res["board_file"] = ws.rel(d / "board.json")
        return res

    def board_source(board: dict | None, board_file: str | None, kind: str) -> Path:
        if bool(board) == bool(board_file):
            raise ToolError("give exactly one of board (a JSON object) or board_file (a workspace path)")
        if board_file:
            return ws_path(board_file)
        d = ws.new_dir(kind)
        p = d / "board.json"
        p.write_text(json.dumps(board, indent=1, ensure_ascii=False), encoding="utf-8")
        return p

    @mcp.tool(annotations=LOCAL)
    async def devo_board_build(
            spec: Annotated[dict[str, Any], Field(description='{"name", "description"?, "from"?, "to"?, "refresh"?, '
                                                              '"widgets": [{"id"?, "type": "Table|Line|Column|Pie|'
                                                              'SimpleValue|Input|…", "query": "<LINQ>", "title"?, '
                                                              '"w"?, "h"?, "x"?, "y"?, "from"?, "to"?, "settings"?}]}. '
                                                              'Layout: 12 columns, default 6x10, left to right')],
            template_file: Annotated[str | None, Field(description="an exported board (devo_board export=true) whose "
                                                                   "widget settings to reuse per subtype")] = None,
            ctx: Context = None) -> dict:
        """Build a board file from a short widget spec (offline). Then devo_board_check run=true, then
        devo_board_push. Read devo_reference dashboards-api.md first."""
        d = ws.new_dir("board")
        (d / "spec.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")
        args = ["board-new", f"--out={d / 'board.json'}"]
        if template_file:
            args.append(f"--template={ws_path(template_file)}")
        args += ["--", str(d / "spec.json")]
        res = package(await engine(args, 120, ctx), out_dir=d)
        return res | {"board_file": ws.rel(d / "board.json")}

    @mcp.tool(annotations=READ)
    async def devo_board_check(
            board_file: Annotated[str | None, Field(description="workspace path of a board file")] = None,
            board: Annotated[dict[str, Any] | None, Field(description="or the board JSON itself")] = None,
            run: Annotated[bool, Field(description="also test-run every widget query (read-only)")] = False,
            inputs: Annotated[dict[str, str] | None, Field(description="values for $NAME.value board inputs")] = None,
            start: Annotated[str | None, Field(description="override the board's range for run")] = None,
            end: str | None = None,
            limit: Annotated[int, Field(ge=1, le=1000)] = 50,
            ctx: Context = None) -> dict:
        """Lint a board file (layout, inputs, LINQ); with run=true test-run each widget's query. Fix
        failures and look into widgets that return 0 rows before publishing."""
        p = board_source(board, board_file, "board")
        args = ["board-check", f"--limit={limit}"]
        opt(args, "--run", run)
        for k, v in (inputs or {}).items():
            args.append(f"--input={k}={v}")
        opt(args, "--from", start)
        opt(args, "--to", end)
        res = await run_or_job("board-check", lambda d: args + ["--", str(p)], 900, False, ctx, lambda r, d: package(r))
        if "job_id" in res:
            return res
        return res | {"board_file": ws.rel(p)}

    # ---------------------------------------------------------------- jobs, workspace, references

    @mcp.tool(annotations=LOCAL)
    async def devo_job_status(job_id: str,
                              cancel: Annotated[bool, Field(description="stop the job")] = False) -> dict:
        """Status of a background job; when it has finished, its output, messages and files."""
        try:
            if cancel:
                runner.cancel_job(job_id)
                await asyncio.sleep(0.2)
            st, d = runner.job_status(job_id)
        except (ValueError, WorkspaceError, OSError) as e:
            raise ToolError(f"unknown job {job_id!r}: {e}")
        if st["state"] in ("running", "lost", "cancelled"):
            progress = d / "stderr.txt"  # streamed while the job runs
            tail = messages(progress.read_text(encoding="utf-8", errors="replace"))[-10:] if progress.exists() else []
            return st | {"command": scrub(st.get("command", "")), "progress": tail}
        out = (d / "stdout.txt").read_text(encoding="utf-8", errors="replace") if (d / "stdout.txt").exists() else ""
        err = (d / "stderr.txt").read_text(encoding="utf-8", errors="replace") if (d / "stderr.txt").exists() else ""
        if st.get("exit_code") != 0:
            return st | {"command": scrub(st.get("command", "")), "messages": messages(err)[-40:],
                         "output_tail": scrub(out[-3000:])}
        parsed: Any = out
        try:
            parsed = json.loads(out) if out.strip()[:1] in "[{" else out
        except json.JSONDecodeError:
            pass
        res = st | {"messages": messages(err), "command": scrub(st.get("command", ""))}
        res["result"], note = fit(scrub_json(parsed), settings.max_output_chars, d, "stdout.full")
        if note:
            res["truncated"] = note
        res["files"] = [ws.rel(p) for p in sorted(d.rglob("*")) if p.is_file()][:200]
        for report in ("health.json", "lag.json", "teams.json", "report.json", "out/batch-summary.json"):
            if (d / report).is_file():
                attach(res, d / report, "report")
                break
        return res

    @mcp.tool(annotations=LOCAL)
    async def devo_workspace_list(dir: Annotated[str | None, Field(description="a workspace dir; omit for the "
                                                                               "top level")] = None) -> dict:
        """Files the tools have written (query exports, sweeps, batch outputs, board files, job dirs).
        Entries older than the workspace TTL are removed automatically."""
        try:
            return {"entries": ws.listing(dir)}
        except WorkspaceError as e:
            raise ToolError(str(e))

    @mcp.tool(annotations=LOCAL)
    async def devo_workspace_read(path: str,
                                  offset: Annotated[int, Field(ge=0, description="first line (0-based)")] = 0,
                                  limit: Annotated[int, Field(ge=1, le=5000, description="lines")] = 200) -> dict:
        """Read a workspace file in pages of lines (.jsonl rows, logs, reports). Follow next_offset."""
        try:
            return ws.read_lines(path, offset, limit, settings.max_output_chars)
        except WorkspaceError as e:
            raise ToolError(str(e))

    @mcp.tool(annotations=LOCAL)
    async def devo_reference(
            name: Annotated[str | None, Field(description="e.g. linq-traps.md, playbooks.md, investigations.md, "
                                                          "table-guide/m365.md, library/INDEX.md, specs; omit to "
                                                          "list all")] = None,
            grep: Annotated[str | None, Field(description="words to find, case-insensitive; separate alternatives "
                                                          "with | (e.g. 'spray|brute force'). Returns matching "
                                                          "lines with context")] = None,
            offset: Annotated[int, Field(ge=0)] = 0,
            limit: Annotated[int, Field(ge=1, le=2000)] = 400) -> dict:
        """Devo knowledge base: LINQ syntax, functions and traps, the Query/Alerts/Activeboards APIs,
        guides to Devo's standard tables, investigation playbooks, and ~580 SecOps detection queries.
        Large files: use grep, or page with offset/next_offset."""
        if not name:
            return {"references": references.catalogue(), "bundled_batch_specs": sorted(references.bundled_specs())}
        if name.strip().rstrip("/") == "specs":
            return {"bundled_batch_specs": {k: v.read_text(encoding="utf-8")
                                            for k, v in references.bundled_specs().items()}}
        try:
            # In a thread: a large file or a slow regex mustn't stall the event loop for other clients.
            return await asyncio.to_thread(references.read, name, grep, offset, limit)
        except ValueError as e:
            raise ToolError(str(e))

    # ---------------------------------------------------------------- writes (opt-in)

    if settings.allow_writes:
        @mcp.tool(annotations=WRITE)
        async def devo_alert_comment(alert_id: str,
                                     title: Annotated[str, Field(min_length=1, max_length=200)],
                                     message: Annotated[str, Field(min_length=1, max_length=20000)],
                                     confirm: Confirm = False, confirmation_token: Token = None,
                                     ctx: Context = None) -> dict:
            """Comment on a triggered alert. Without confirm: a preview (the alert's title and status
            and the exact request); nothing is written. The comment is posted as the token's user and
            seen by everyone who works the alert: write it as that analyst, no tool attribution."""
            if not re.fullmatch(r"\d+", alert_id.strip()):
                raise ToolError("alert_id must be numeric")
            params = {"id": alert_id.strip(), "title": title, "msg": message}
            args = ["comment", f"--title={title}", f"--msg={message}", "--", params["id"]]
            return await write_action("comment", params, args, confirm, confirmation_token, ctx)

        @mcp.tool(annotations=DESTRUCTIVE)
        async def devo_board_push(board_file: Annotated[str | None, Field(description="workspace board file")] = None,
                                  board: Annotated[dict[str, Any] | None, Field(description="or the board JSON")] = None,
                                  board_id: Annotated[str | None, Field(description="replace this board (PUT, the "
                                                                                    "whole board); omit to create "
                                                                                    "a new private board")] = None,
                                  name: str | None = None, description: str | None = None,
                                  confirm: Confirm = False, confirmation_token: Token = None,
                                  ctx: Context = None) -> dict:
            """Create or replace an Activeboard. Without confirm: the exact request and, for a
            replacement, the widget diff; nothing is sent. A backup of a replaced board is kept in
            the domain cache. Never touch boards other people own unless the user names them."""
            p = board_source(board, board_file, "board")
            content = hashlib.sha256(p.read_bytes()).hexdigest()
            # Bound to the content, not the path: an inline board is written to a new file on every call.
            params = {"sha": content, "id": board_id, "name": name, "description": description}
            args = ["board-push"]
            opt(args, "--id", board_id and board_ref(board_id))
            opt(args, "--name", name)
            opt(args, "--description", description)
            args += ["--", str(p)]
            return await write_action("board-push", params, args, confirm, confirmation_token, ctx) | \
                {"board_file": ws.rel(p)}

        @mcp.tool(annotations=WRITE)
        async def devo_board_set(board_id: str,
                                 private: bool | None = None,
                                 tags: Annotated[list[str] | None, Field(description="replaces the tags")] = None,
                                 favorite: bool | None = None, default: bool | None = None,
                                 confirm: Confirm = False, confirmation_token: Token = None,
                                 ctx: Context = None) -> dict:
            """Set a board's privacy, tags, favourite or default flag. Preview unless confirmed."""
            params = {"id": board_id, "private": private, "tags": tags, "favorite": favorite, "default": default}
            args = ["board-set"]
            for flag, v in (("--private", private), ("--favorite", favorite), ("--default", default)):
                if v is not None:
                    args += [flag, "true" if v else "false"]
            if tags is not None:
                args.append(f"--tags={', '.join(tags)}")
            args += ["--", board_ref(board_id)]
            return await write_action("board-set", params, args, confirm, confirmation_token, ctx)

        @mcp.tool(annotations=WRITE)
        async def devo_board_clone(board_id: str, name: str, description: str | None = None,
                                   confirm: Confirm = False, confirmation_token: Token = None,
                                   ctx: Context = None) -> dict:
            """Copy a board under a new name. Preview unless confirmed."""
            params = {"id": board_id, "name": name, "description": description}
            args = ["board-clone", f"--name={name}"]
            opt(args, "--description", description)
            args += ["--", board_ref(board_id)]
            return await write_action("board-clone", params, args, confirm, confirmation_token, ctx)

        @mcp.tool(annotations=DESTRUCTIVE)
        async def devo_board_delete(board_id: str, confirm: Confirm = False, confirmation_token: Token = None,
                                    ctx: Context = None) -> dict:
            """Delete a board (a local backup is kept in the domain cache). Preview unless confirmed."""
            return await write_action("board-delete", {"id": board_id}, ["board-delete", "--", board_ref(board_id)], confirm,
                                      confirmation_token, ctx)

    # ---------------------------------------------------------------- resources and prompts

    def add_resource(uri: str, path: Path, desc: str) -> None:
        mime = "application/json" if path.suffix in (".json", ".jsonl") else "text/markdown"
        mcp.resource(uri, name=uri.removeprefix("devo://"), description=desc, mime_type=mime)(
            lambda: path.read_text(encoding="utf-8"))

    add_resource("devo://guide", ENGINE_DIR / "SKILL.md",
                 "The original Devo skill guide (written for the devo.py CLI: each command maps to a devo_* tool)")
    for f in references.all_files():
        if f.endswith(".md"):
            add_resource(f"devo://reference/{f}", ENGINE_DIR / "references" / f,
                         references.DESCRIPTIONS.get(f, f))

    @mcp.prompt(description="Triage a Devo alert to a verdict with evidence and a drafted comment")
    def triage_alert(alert_id: str) -> str:
        return (f"Triage Devo alert {alert_id}. Read devo_reference name=investigations.md (sections on triage, "
                "the per-alert checklists and the report template) first. Then devo_alert for the definition, "
                "detection query, decoded evidence and comments; establish native vs forwarded, when it really "
                "happened (raw event time vs alert time), who (take entities from the evidence, not mapped_user), "
                "and the rule's scope (sibling tables). Reproduce the detection query around the event time, "
                "pivot on each entity and baseline the previous 7 days. Report: verdict, confidence, evidence "
                "(queries + UTC ranges + counts), scope, recommended actions, and a drafted comment. Do not post "
                "the comment unless I approve its exact text.")

    @mcp.prompt(description="Reconstruct everything a user, IP or host did over a window")
    def investigate_entity(entity: str, kind: Literal["user", "ip", "host"] = "user", window: str = "7d") -> str:
        steps = {"user": f"devo_activity terms=['{entity}'] by=user expand=true start={window}, then devo_timeline "
                         "on its dir; for Teams detail devo_teams.",
                 "ip": f"devo_activity terms=['{entity}'] by=ip start={window}, then devo_timeline.",
                 "host": f"devo_coverage name={entity} first (is it logging at all?), then devo_activity "
                         f"terms=['{entity}'] by=host start={window}, then devo_timeline."}[kind]
        return (f"Investigate {kind} {entity} over {window}. Read devo_reference name=playbooks.md. {steps} Report "
                "per day from the direct events, check target/automated/raw-text events, flag FEED GAP sources as "
                "gaps not inactivity, list sources checked that had no data, and separate what the data shows "
                "from what is inferred.")

    @mcp.prompt(description="Health check of the Devo domain's log sources and collectors")
    def domain_health() -> str:
        return ("Run devo_health (background) and, while it runs, devo_lag. When the job finishes, read "
                "devo_reference name=playbooks.md (health section) and report: sources stopped, dropped or new; "
                "quiet senders; collector errors; lag outliers, each with the evidence and a suggested action.")

    mcp.devo_warmer = warmer  # started by the transport (cli.py), once an event loop runs
    return mcp
