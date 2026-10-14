# CLAUDE.md

## What this repo is

An MCP server (Python, official `mcp` SDK 2.x: `MCPServer`, not `FastMCP`) that exposes the Devo
SIEM to any MCP client. The Devo logic is not written here. It is the `devo` skill's helper,
vendored into `src/devo_mcp/engine/` and run as a subprocess per tool call.

## Architecture (the part that isn't obvious from the files)

- **`src/devo_mcp/engine/` is vendored. Don't edit it here.** Fix bugs in
  `secops-skills/plugins/devo/skills/devo`, then run `scripts/sync-engine.sh` (it rewrites the
  whole directory and `VENDORED.json`). Local edits are lost on the next sync.
- **Subprocess per call is deliberate.** The engine keeps module-level state (the active cache,
  warnings), prints to stdout/stderr, and is tested as a CLI. Running it in-process would mix
  concurrent HTTP requests. `engine_runner.py` runs it with `python -I`, a scrubbed environment
  (no `MCP_API_KEY`), its own process group (so a kill takes its children too), and two
  semaphores: one for quick `engine()` calls, one for jobs (every `run_or_job` call, since one
  handed off may run for hours).
- **Tools map to engine commands.** Each `devo_*` tool builds an argv for `devo.py <cmd>`, and
  `package()` turns the result into JSON: `result`, `messages` (the engine's `#` stderr lines,
  which carry row counts, UTC ranges and limit warnings), and `files`. A non-zero exit becomes a
  tool error that includes the engine's ERROR/hint lines. Engine text that says `devo.py <cmd>`
  means the `devo_<cmd>` tool. The server instructions say this too.
- **Argv rules (security).** Client-supplied values are passed as a single `--flag=value` token
  (`opt()`), and positionals go after a `--` separator, so a value that starts with `-` can never
  turn into an engine option (`--from-file`, `--write`, `--map` would read or write any file).
  Table names and board ids are validated too (`table_name()`, `board_ref()`). Batch jobs from a
  client (inline or a workspace spec) go through `checked_jobs()` and are rewritten into the job
  dir: the engine's `query_file` reads any path, so only bundled specs may use it. Keep to these
  rules in new tools. `test_option_injection_refused` and the `test_batch_*` tests cover them, and
  `FakeEngine` parses every argv with the engine's real argparse parser.
- **Timeouts.** A tool's `timeout` is the engine's per-query `--timeout`. Anything that can be
  slow goes through `run_or_job`: it runs as a job (job pool) and waits only up to
  `DEVO_MCP_SOFT_TIMEOUT_SECONDS` (45 s). After that it returns the `job_id` and the job finishes
  in the background, because MCP clients drop calls after about 60 s. Jobs are killed at
  `DEVO_MCP_MAX_JOB_SECONDS`, and fast single-query calls (`engine()`) at `timeout + GRACE`.
- **Workspace.** Every file a tool reads or writes lives under `DEVO_MCP_WORKDIR`, and clients
  only see workspace-relative paths (`Workspace.resolve` refuses traversal). `scrub()` rewrites
  server paths in engine output. Background jobs are `job-*` directories with `status.json`.
- **Writes** are registered only when `DEVO_MCP_ALLOW_WRITES` is set. `write_action()` sends
  `--confirm` only with an HMAC `confirmation_token` from a preview of the same parameters (and,
  for boards, the same file hash). The key is random per process.
- **Cache warm-up.** `cache_warmer.py` runs the engine's incremental `cache build` as a background
  job once the event loop starts (stdio: `cli.py`; HTTP: the ASGI lifespan startup in
  `WithHealthz`), then every `DEVO_MCP_CACHE_CHECK_HOURS`. The engine owns the TTLs (fields 30 days,
  tables 7). `Settings()` defaults `cache_warmup` off so tests never start it; `from_env` defaults it on.
- **HTTP** is stateless streamable HTTP mounted at `/mcp`, behind `BearerAuth`, with an open
  `/healthz`. The CLI refuses HTTP without `MCP_API_KEY` unless `DEVO_MCP_ALLOW_NO_AUTH=true`.
- **`MCP_TOOL_ERRORS_AS_TEXT`** (off by default) adds `ToolErrorsAsText` (`tool_errors.py`), server
  middleware that rewrites every `isError: true` `tools/call` result as `isError: false` with
  `ERROR: <message>`, because Foundry Agent Service aborts the run on `isError`. Tools raise
  `ToolError` as usual; don't special-case the mode in a tool.

## Commands

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest                 # offline; the engine is faked
.venv/bin/python scripts/smoke.py          # live, read-only, over stdio (needs a Devo token)
docker build -t devo-mcp . && docker run --rm -p 8000:8000 -e MCP_API_KEY -e DEVO_TOKEN devo-mcp
```

## Live testing

The token on dev machines points at a **live** Devo domain. Only call read-only tools, over
short windows and with aggregated queries. Test write tools in preview only: never pass
`confirm=true` against the live domain unless the user asks for that specific change.

## Adding a tool

1. Find the engine command and its flags (`python3 src/devo_mcp/engine/scripts/devo.py <cmd> -h`).
2. Add the tool in `server.py`. Prefer the engine's JSON output (`--format json`, `--json`, `--out`)
   and `parse_json=True` or `attach()`. Give long commands `background` through `run_or_job`.
3. Set the annotations (`READ`, `LOCAL`, `WRITE`, `DESTRUCTIVE`). Anything that changes Devo goes
   inside `if settings.allow_writes:` and through `write_action()`.
4. Add an offline test with `FakeEngine` (`tests/test_server.py`) and, if it's read-only, a
   smoke call.
