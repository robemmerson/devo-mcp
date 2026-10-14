# devo-mcp

An MCP server for the **Devo SIEM**. It does what the `devo` Claude skill (in
[secops-skills](https://github.com/robemmerson/secops-skills)) does, but for any MCP client: Claude
Code, Claude Desktop, n8n, and other agent harnesses. It works with any Devo domain: on start-up it
profiles the domain's own tables and fields, so it learns what your environment holds.

- LINQ queries over any time range, with chunking, auto-split, and server-side merging.
- Picking tables and fields from a cached, per-domain profile.
- Alert triage: alert lists, one decoded alert with its detection query, and definitions.
- Investigations: everything a user, IP or host did (`activity` → `timeline`), Teams reconstruction,
  who logged on where, credential attacks, and privileged role grants (PIM).
- Log-source health: per-host coverage, domain health, and ingestion lag.
- Activeboards (dashboards): list, read, build and test.
- Optional writes, off by default: alert comments and board changes. Each one needs a preview and
  an explicit confirm.

## How it works

The skill's helper (`devo.py`, Python standard library only) and its reference library are
vendored into `src/devo_mcp/engine/`. Each tool call runs the helper as its own subprocess. That
keeps every API workaround the skill has learned (the Cloudflare user agent, IP decoding, chunk
retries, cache healing, and so on). It also isolates concurrent requests and lets a timeout kill
a stuck query.

```
client ──MCP──▶ devo_mcp.server ──subprocess──▶ engine/scripts/devo.py ──HTTPS──▶ Devo APIs
                     │                                  │
                     ├─ workspace (query exports, sweeps, jobs)      └─ domain cache (~/.cache/devo-skill)
                     └─ references (LINQ, tables, playbooks, ~580 detections) as tools + resources
```

To update the engine from the skill, run `scripts/sync-engine.sh [path/to/secops-skills]`.
`engine/VENDORED.json` records the source commit, and `devo_check` reports it.

## Tools

| Area | Tools |
|---|---|
| Session and discovery | `devo_check`, `devo_tables`, `devo_schema`, `devo_fields`, `devo_profile`, `devo_cache` |
| Queries | `devo_query`, `devo_batch` |
| Alerts | `devo_alerts`, `devo_alert`, `devo_alert_definitions` |
| Investigations | `devo_activity`, `devo_timeline`, `devo_teams`, `devo_logons`, `devo_coverage`, `devo_creds`, `devo_grants` |
| Health | `devo_health`, `devo_lag` |
| Dashboards | `devo_boards`, `devo_board`, `devo_board_build`, `devo_board_check` |
| Plumbing | `devo_job_status`, `devo_workspace_list`, `devo_workspace_read`, `devo_reference` |
| Writes (opt-in) | `devo_alert_comment`, `devo_board_push`, `devo_board_set`, `devo_board_clone`, `devo_board_delete` |

- **Prompts:** `triage_alert`, `investigate_entity` and `domain_health`.
- **Resources:** `devo://guide` and `devo://reference/<file>.md` (LINQ syntax, functions and traps,
  the APIs, table guides, playbooks, and the detection library). Clients that don't read
  resources, such as n8n, get the same material through `devo_reference`.
- **Instructions:** the server's instructions carry the skill's working rules, such as the query
  workflow, the LINQ traps, and the triage steps.

### Results, files and long runs

- **Inline results.** Results are JSON. Anything larger than `DEVO_MCP_MAX_OUTPUT_CHARS` is cut
  to fit, and the whole result is saved to the workspace. The result names that file, and
  `devo_workspace_read` pages through it.
- **Files.** Query exports (`save=true`), activity sweeps, batch outputs and board files are
  written to a server-side workspace. Tools refer to them by workspace-relative paths. Entries
  older than `DEVO_MCP_WORK_TTL_HOURS` are deleted.
- **Calls that outlive the client's timeout.** Most MCP clients give up on a tool call after about
  60 s. So queries, sweeps and reports run as jobs, and the server waits for them only up to
  `DEVO_MCP_SOFT_TIMEOUT_SECONDS` (45 s by default). A call that finishes in time returns its
  result as normal. A slower one returns `{"state": "running", "job_id": …}` and keeps running,
  and the agent polls `devo_job_status` for the same result.
- **Long runs.** Slow tools accept `background=true`. They return a `job_id` at once, and you
  poll it with `devo_job_status`. While a call runs in the foreground, the server sends a
  progress notification every 15 s.

### Writes

Writes are disabled unless `DEVO_MCP_ALLOW_WRITES=true`. When they're enabled:

1. Called without `confirm`, a write tool only previews. Nothing is sent. The response holds the
   exact request, the alert's title and status or the board's widget diff, and a
   `confirmation_token`.
2. `confirm=true` works only with the token from a preview of **the same parameters** (for
   boards, the same file content). Each token confirms one change and expires after about 30–60
   minutes. So a client can't skip the preview, or send something other than what was previewed.
3. The tools never change alert status, priority or definitions.

The token proves that a preview came first, not that a person approved it: a client that can
call the tools can also confirm. Human approval comes from your MCP client's tool-permission
prompt, so keep writes on "ask" there (the write tools are marked as writes, and the board push
and delete as destructive). Every client with the `MCP_API_KEY` has the same access, so enable
writes only on a server whose clients you trust to make changes in Devo.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DEVO_TOKEN` | – | Devo API token. If unset, it's read from `DEVO_ENV_FILE`, then from `~/.config/devo/env` |
| `DEVO_REGION` | `eu` | `eu`, `us`, `ca`, `apac` or `us3` |
| `DEVO_ENV_FILE` | `~/.config/devo/env` | `KEY=VALUE` file with the variables above |
| `DEVO_CACHE_KEY` | hash of the token | Names the domain cache, so it survives token rotation |
| `DEVO_CACHE_DIR` | `~/.cache/devo-skill` | Domain cache. Shared with the skill on the same machine |
| `MCP_API_KEY` | – | **Required for HTTP.** Clients send `Authorization: Bearer <key>`. Use 24+ random characters |
| `DEVO_MCP_ALLOW_WRITES` | `false` | Expose the comment and board write tools |
| `DEVO_MCP_WORKDIR` | `~/.local/state/devo-mcp/work` | Workspace for files the tools write |
| `DEVO_MCP_WORK_TTL_HOURS` | `48` | Workspace entries older than this are deleted |
| `DEVO_MCP_MAX_OUTPUT_CHARS` | `40000` | Size limit for an inline result |
| `DEVO_MCP_MAX_CONCURRENCY` | `4` | Engine processes at once. Interactive calls and background jobs each get their own pool of this size |
| `DEVO_MCP_SOFT_TIMEOUT_SECONDS` | `45` | A slow call returns `{"state": "running", "job_id"}` after this long and finishes in the background (`0` = wait as long as it takes) |
| `DEVO_MCP_MAX_CALL_SECONDS` | `3600` | With the soft timeout off: wall-clock limit for a call that runs many queries (`timeout` is per query) |
| `DEVO_MCP_MAX_JOB_SECONDS` | `21600` | Wall-clock limit for a background job |
| `DEVO_MCP_CACHE_WARMUP` | `true` | Build the domain cache in the background at start-up, then keep it fresh (see "Domain cache") |
| `DEVO_MCP_CACHE_PROFILE` | `all` | Tables the warm-up profiles: `all`, `none`, or the N busiest |
| `DEVO_MCP_CACHE_CHECK_HOURS` | `24` | How often the warm-up runs again (`0` = only at start-up) |
| `DEVO_MCP_CACHE_PARALLEL` | `6` | Tables profiled at once during warm-up |
| `DEVO_CACHE_MAX_AGE_DAYS` | – | Caps every cache TTL (engine setting) |
| `DEVO_MCP_ALLOWED_HOSTS` | – | Comma-separated `Host` values. Turns on DNS-rebinding protection for HTTP |
| `MCP_TOOL_ERRORS_AS_TEXT` | `false` | Return tool errors as `isError: false` results whose text is `ERROR: <message>`. For clients that end the run on `isError: true` (Microsoft Foundry Agent Service), so the model sees the error and retries. Each one is logged as a warning |
| `DEVO_MCP_ALLOW_NO_AUTH` | `false` | Serve HTTP without `MCP_API_KEY`. Use only behind an authenticating proxy |
| `DEVO_MCP_TRANSPORT` / `_HOST` / `_PORT` / `_PATH` | `stdio` / `127.0.0.1` / `8000` / `/mcp` | CLI defaults |

The server never reads or logs the Devo token itself: it passes the environment to the engine,
with `MCP_API_KEY` removed.

## Domain cache

The engine caches what it learns about your domain. The cache lives in `DEVO_CACHE_DIR`, which
is `/data/cache` in the image. Cached entries are:

- the table list
- each table's schema and field profile (type, fill rate, value pattern and role for each field;
  shapes only, never values)
- event-time expressions, verified against real rows
- the validated sources for the `activity` sweep

Tools such as `devo_fields`, `devo_activity` and `devo_timeline` read the cache. Without it they
profile tables live on first use.

| Entry | Expires |
|---|---|
| table list, sweep sources | 7 days |
| schemas, field profiles, verified event time | 30 days |
| notes, naming templates | never (flagged for re-verification after 90 days) |

The server keeps the cache warm by itself:

- **At start-up.** About 10 s after start-up it runs `cache build` as a background job.
- **First run.** On an empty cache, that job profiles every table that has data, sampling 300 rows
  each. Expect several minutes on a large domain.
- **Later runs.** The job runs again every `DEVO_MCP_CACHE_CHECK_HOURS`. Builds are incremental:
  they only fetch entries that are missing or past their expiry, such as new tables or profiles
  older than 30 days, so a daily run is cheap.
- **Progress.** `devo_check` reports the warm-up's state, its job id (follow it with
  `devo_job_status`), and when it last ran. A manual `devo_cache action=build` started while the
  warm-up is running returns the warm-up's job instead of starting a second build.
- **Forcing a refresh.** `devo_cache action=refresh args=["fields"]` re-profiles everything now,
  even entries that are still fresh. `action=verify` compares the cached schemas with the live
  ones and re-profiles the tables that changed.

**Keep `/data` on a persistent volume.** Without one, every container restart starts with an empty
cache and profiles everything again.

## Running it

### Local (stdio): Claude Code and Claude Desktop

```sh
uv venv && uv pip install -e .        # or: pipx install .
```

Claude Code:

```sh
claude mcp add devo -- /path/to/devo-mcp/.venv/bin/devo-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "devo": {
      "command": "/path/to/devo-mcp/.venv/bin/devo-mcp",
      "env": { "DEVO_REGION": "eu" }
    }
  }
}
```

The token comes from `~/.config/devo/env` (`DEVO_TOKEN=…` and `DEVO_REGION=…` lines; the `devo`
skill uses the same file). You can also put `DEVO_TOKEN` in `env`.

### HTTP: n8n and other remote clients

```sh
export MCP_API_KEY=$(openssl rand -hex 32)
devo-mcp --transport http --host 0.0.0.0 --port 8000
```

- Endpoint: `http://<host>:8000/mcp` (streamable HTTP, stateless). `GET /healthz` is
  unauthenticated, for load balancers.
- n8n: in the **MCP Client Tool** node, set the endpoint to the URL above, the transport to
  *HTTP Streamable*, and authentication to *Header Auth* with name `Authorization` and value
  `Bearer <MCP_API_KEY>`.
- `--json-response` returns plain JSON instead of SSE streams. Use it for clients or proxies that
  can't handle SSE (you lose progress notifications).

### Docker

Each release is published to the GitHub Container Registry as `ghcr.io/robemmerson/devo-mcp`
(`:X.Y.Z`, `:X.Y` and `:latest`, for `linux/amd64`):

```sh
docker run -d --name devo-mcp -p 127.0.0.1:8000:8000 \
  -e MCP_API_KEY -e DEVO_TOKEN -e DEVO_REGION=eu \
  -v devo-mcp-data:/data ghcr.io/robemmerson/devo-mcp:latest
```

To build it yourself instead: `docker build -t devo-mcp .`

- The image runs as uid 10001.
- `/data` holds the domain cache and the workspace. Keep it on a volume, so the cache (which takes
  minutes to build on a large domain) survives restarts.
- Credentials come only from the runtime environment.
- To use an env file instead of `DEVO_TOKEN`, mount it, set `DEVO_ENV_FILE`, and make it readable
  by uid 10001 (or run the container with `--user` as the file's owner).

See `compose.yaml` for a compose setup.

## Contributing

Contributions are welcome, especially guidance and helpers for log sources the server doesn't know
well yet. The aim is a shared library for everything that sends data to Devo, not only the sources
it started with. Today the curated knowledge is deepest for Microsoft 365, Entra ID and Defender,
Windows (NxLog), Linux, Fortinet, Zscaler ZPA, SentinelOne, Qualys, AWS CloudTrail, GitHub and
1Password. Other tables still work, because the server profiles every table it finds, but without
curated hints.

A log source can gain any of these (paths under `src/devo_mcp/engine/references/`):

| File | What it adds |
|---|---|
| `hints/activity-sources.json` | A sweep recipe, so `devo_activity` finds a user, IP or host in the table |
| `hints/event-time.json` | Which field holds the real event time, dedupe keys, and default lag tables |
| `hints/field-roles.json` | Field-role fixes (user, ip, host, join-id…) where the profiler guesses wrong |
| `table-guide/*.md` | What a row is, the entity fields, quirks, and how records link to other tables |
| `specs/*.jsonl` and `*.linq` | Batch playbooks for `devo_batch` |

Helpers that need code go in `scripts/devo.py` (a new command) and `server.py` (a `devo_*` tool).

**Where to send it.** The engine (`src/devo_mcp/engine/`) is maintained in
[secops-skills](https://github.com/robemmerson/secops-skills) under `plugins/devo/skills/devo` and
copied here by `scripts/sync-engine.sh`, which overwrites the directory. Send engine changes there,
or open them here and they'll be applied upstream and synced back. Changes to the server, its
tools, the Docker image or CI belong here. To suggest a source without writing it yourself, open a
**Log source** issue.

**Keep your own data out.** Never put real events, usernames, host names, IPs, tenant or account
ids from your domain in an issue, pull request, test or example. Describe field *shapes*:
`devo_fields` reports type, fill rate, pattern and role, never values. Use placeholders such as
`jsmith`, `example.com`, `HOST01`, `10.1.2.3` and the documentation ranges `192.0.2.0/24`,
`198.51.100.0/24` and `203.0.113.0/24`.

**Before you open a pull request:**

- Run the offline tests (see Development); a new tool needs a `FakeEngine` test.
- Pass client values to the engine as one `--flag=value` token, with positionals after `--`.
- Give the PR a [Conventional Commits](https://www.conventionalcommits.org) title (`feat:`,
  `fix:`, `docs:`…), since it sets the release version.

Report security issues privately through the repository's **Security** tab, not in a public issue.

## Development

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest              # offline tests (the engine is faked)
.venv/bin/python scripts/smoke.py       # read-only calls against the live domain over stdio
.venv/bin/python scripts/smoke.py --url http://127.0.0.1:8000/mcp   # …or against an HTTP server
```

`smoke.py` calls only read-only tools, with short windows and aggregated queries.

## Releases

Releases are automatic. Each merge to `main` runs `.github/workflows/release.yml`:

1. It runs the tests, then works out the next version from the
   [Conventional Commits](https://www.conventionalcommits.org) since the last `vX.Y.Z` tag
   (`scripts/release.py plan`):
   - `feat!:` or `BREAKING CHANGE:` bumps the major version (the minor while on 0.x).
   - `feat:` bumps the minor version.
   - `fix:`, `perf:`, `refactor:`, `revert:`, `build:` and `(deps)` bump the patch version.
   - A merge with only `docs:`, `ci:`, `test:` or `chore:` commits doesn't release.
2. It builds the image for `linux/amd64` and pushes it to `ghcr.io/robemmerson/devo-mcp` as
   `:X.Y.Z`, `:X.Y`, `:latest` and `:sha-<commit>`, with provenance and an SBOM.
3. It tags the merge commit `vX.Y.Z`, publishes a GitHub release with the notes, labels the merged
   PR `released`, and comments the release and `docker pull` line on it.

The version is stamped into the image at build time. The files in the repo keep the base version.

The workflow pushes with the repository's own `GITHUB_TOKEN`; it needs no other secrets. Pull requests run
`.github/workflows/ci.yml`: the tests, a Docker build, and a check that the container starts and
refuses unauthenticated requests.


## Licence

[MIT](LICENSE). The detection library, table catalogue and union-table reference in the engine
come from Devo's documentation and stay under Devo's terms: see [THIRD_PARTY.md](THIRD_PARTY.md).
