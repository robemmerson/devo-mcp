"""Runs the vendored Devo helper (engine/scripts/devo.py) as a subprocess per call.

A subprocess per call keeps the engine exactly as it is tested in the skill (it keeps module-level
state such as the active cache), isolates concurrent requests from each other, and lets a timeout
kill a stuck query outright. Long calls can also run as background jobs that a client polls.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import Workspace

ENGINE_DIR = Path(__file__).resolve().parent / "engine"
ENGINE = ENGINE_DIR / "scripts" / "devo.py"
REFERENCES = ENGINE_DIR / "references"

EXIT_MEANING = {1: "usage or configuration error", 2: "Devo API error or bad arguments", 3: "timeout",
                124: "killed by the MCP server after its timeout"}
# Variables the engine must never see: the MCP server's own secret.
SCRUB_ENV = ("MCP_API_KEY",)
GRACE = 30  # seconds a single-query call may run past the engine's own --timeout before it is killed


@dataclass
class EngineResult:
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def engine_env() -> dict[str, str]:
    # The engine runs with `python -I`, which ignores PYTHON* variables; it sets UTF-8 stdio itself.
    return {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}


def describe(args: list[str]) -> str:
    """The command line for a result, shortened so a huge LINQ doesn't swamp it."""
    shown = [a if len(a) <= 300 else a[:300] + "…" for a in args]
    return "devo.py " + " ".join(json.dumps(a) if (" " in a or not a) else a for a in shown)


class EngineRunner:
    def __init__(self, python: str, workspace: Workspace, max_concurrency: int = 4):
        self.python = python
        self.workspace = workspace
        self._sem = asyncio.Semaphore(max_concurrency)
        self._job_sem = asyncio.Semaphore(max_concurrency)  # jobs never starve interactive calls
        self._jobs: dict[str, asyncio.Task] = {}

    async def run(self, args: list[str], limit: int, stdin: str | None = None, job: bool = False,
                  log_dir: Path | None = None) -> EngineResult:
        """Run the engine, killing it after `limit` wall-clock seconds. The engine's own --timeout is
        per query; commands that run many queries (chunks, batch jobs, sweep sources) need a limit
        well above it."""
        async with (self._job_sem if job else self._sem):
            if log_dir is not None:
                return await self._exec(args, limit, stdin, log_dir=log_dir)
            return await self._exec(args, limit, stdin)

    async def _exec(self, args: list[str], limit: int, stdin: str | None, log_dir: Path | None = None) -> EngineResult:
        """With log_dir, stdout/stderr stream into log_dir/stdout.txt and stderr.txt as the engine
        writes them (so a running job's progress can be read); otherwise they are piped."""
        if log_dir is not None:
            return await self._exec_logged(args, limit, log_dir)
        t0 = time.monotonic()
        kw: dict[str, Any] = {}
        if os.name != "nt":
            kw["start_new_session"] = True  # own process group: a kill takes the engine's children too
        proc = await asyncio.create_subprocess_exec(
            self.python, "-I", str(ENGINE), *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=engine_env(), cwd=str(self.workspace.root), **kw)
        # Read both pipes as the engine writes them, so a kill keeps whatever it printed before.
        reads = [asyncio.ensure_future(proc.stdout.read()), asyncio.ensure_future(proc.stderr.read())]
        try:
            if stdin is not None:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            code = await asyncio.wait_for(proc.wait(), limit)
            out, err = await asyncio.gather(*reads)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            self._kill(proc)
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                for t in reads:
                    t.cancel()
                raise
            out, err = await asyncio.gather(*reads)
            code = 124
            err += f"\nERROR: killed by the MCP server after {limit}s; use background=true or a narrower " \
                   "window".encode()
        return EngineResult(args, code if code is not None else -1, out.decode("utf-8", "replace"),
                            err.decode("utf-8", "replace"), round(time.monotonic() - t0, 1))

    async def _exec_logged(self, args: list[str], limit: int, log_dir: Path) -> EngineResult:
        t0 = time.monotonic()
        kw: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {}
        out_p, err_p = log_dir / "stdout.txt", log_dir / "stderr.txt"
        with open(out_p, "wb") as out_f, open(err_p, "wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                self.python, "-I", str(ENGINE), *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=out_f, stderr=err_f, env=engine_env(), cwd=str(self.workspace.root), **kw)
            try:
                code = await asyncio.wait_for(proc.wait(), limit)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                self._kill(proc)
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                code = 124
                err_f.write(f"\nERROR: killed by the MCP server after {limit}s (DEVO_MCP_MAX_JOB_SECONDS)\n".encode())
        return EngineResult(args, code, out_p.read_text(encoding="utf-8", errors="replace"),
                            err_p.read_text(encoding="utf-8", errors="replace"), round(time.monotonic() - t0, 1))

    @staticmethod
    def _kill(proc) -> None:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------ background jobs

    def start_job(self, kind: str, args_for_dir, limit: int) -> dict:
        """Start a run as a job, in the job pool. args_for_dir(job_dir) returns the engine
        arguments, so outputs land in the job's own directory."""
        d = self.workspace.new_dir(f"job-{kind}")
        try:
            args = args_for_dir(d)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        job_id = d.name
        status = {"job_id": job_id, "state": "running", "kind": kind, "command": describe(args),
                  "started_utc": _now(), "dir": self.workspace.rel(d)}
        _write_json(d / "status.json", status)

        async def go():
            try:
                r = await self.run(args, limit, job=True, log_dir=d)
                for name, text in (("stdout.txt", r.stdout), ("stderr.txt", r.stderr)):
                    if not (d / name).exists():  # a replaced runner (tests) may not stream
                        (d / name).write_text(text, encoding="utf-8")
                status.update(state="done" if r.ok else "failed", exit_code=r.exit_code,
                              seconds=r.seconds, finished_utc=_now())
                return r
            except asyncio.CancelledError:
                status.update(state="cancelled", finished_utc=_now())
                raise
            except Exception as e:  # report, never lose the job silently
                status.update(state="failed", error=repr(e), finished_utc=_now())
            finally:
                _write_json(d / "status.json", status)
                self._jobs.pop(job_id, None)

        self._jobs[job_id] = asyncio.get_running_loop().create_task(go())
        return status

    def job_status(self, job_id: str) -> tuple[dict, Path]:
        if not job_id.startswith("job-"):
            raise ValueError(f"{job_id!r} is not a job id")
        d = self.workspace.resolve(job_id)
        status = json.loads((d / "status.json").read_text(encoding="utf-8"))
        if status.get("state") == "running" and job_id not in self._jobs:
            status["state"] = "lost"  # the server restarted while it ran
            status["error"] = "the server restarted before this job finished; start it again"
        return status, d

    def job_task(self, job_id: str) -> asyncio.Task | None:
        """The running job's task; its result is the EngineResult (None if it crashed)."""
        return self._jobs.get(job_id)

    async def wait_job(self, job_id: str) -> None:
        t = self._jobs.get(job_id)
        if t is not None:
            try:
                await asyncio.shield(t)
            except Exception:
                pass  # the job records its own failure in status.json

    def cancel_job(self, job_id: str) -> bool:
        t = self._jobs.get(job_id)
        if t and not t.done():
            t.cancel()
            return True
        return False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)
