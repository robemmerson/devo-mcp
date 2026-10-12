"""Keeps the engine's domain cache warm: profiles every table once, then keeps it fresh.

`devo.py cache build` is incremental. It fetches only entries that are missing or past their TTL:
the table list and sweep sources after 7 days; schemas, field profiles and verified event time
after 30 (DEVO_CACHE_MAX_AGE_DAYS caps these). So the warmer runs it shortly after start-up and then
every DEVO_MCP_CACHE_CHECK_HOURS. The first run on an empty cache takes minutes; later runs only
pick up new tables and expiring entries. Each run is an ordinary background job, visible through
devo_job_status.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .engine_runner import EngineRunner
from .settings import Settings

log = logging.getLogger("devo_mcp.cache_warmer")


class CacheWarmer:
    def __init__(self, runner: EngineRunner, settings: Settings, enabled_reason: str | None = None):
        self.runner = runner
        self.settings = settings
        self._task: asyncio.Task | None = None
        enabled = settings.cache_warmup and enabled_reason is None
        self.status: dict = {"enabled": enabled, "state": "waiting" if enabled else "disabled",
                             "profile": settings.cache_profile, "every_hours": settings.cache_check_hours}
        if not settings.cache_warmup:
            self.status["reason"] = "DEVO_MCP_CACHE_WARMUP=false"
        elif enabled_reason:
            self.status["reason"] = enabled_reason

    def start(self) -> None:
        """Start the warm-up loop on the running event loop (idempotent)."""
        if self.status["enabled"] and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="devo-cache-warmer")

    @property
    def running_job(self) -> str | None:
        return self.status.get("job_id") if self.status.get("state") == "running" else None

    def args(self) -> list[str]:
        return ["cache", f"--profile={self.settings.cache_profile}", f"--parallel={self.settings.cache_parallel}",
                "--", "build"]

    async def run_once(self) -> dict:
        st = self.runner.start_job("cache-warm", lambda d: self.args(), self.settings.max_job_seconds)
        self.status.update(state="running", job_id=st["job_id"], last_started_utc=st["started_utc"])
        self.status.pop("next_run_utc", None)
        log.info("cache warm-up started: job %s", st["job_id"])
        await self.runner.wait_job(st["job_id"])
        final, d = self.runner.job_status(st["job_id"])
        summary = ""
        err = d / "stderr.txt"
        if err.exists():  # the engine's last progress line says what it fetched
            lines = [ln for ln in err.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            summary = lines[-1][:300] if lines else ""
        self.status.update(state="idle" if final["state"] == "done" else "failed",
                           last_result=final["state"], last_finished_utc=final.get("finished_utc"),
                           last_seconds=final.get("seconds"), last_message=summary)
        log.info("cache warm-up %s in %ss: %s", final["state"], final.get("seconds"), summary)
        return final

    async def _loop(self) -> None:
        await asyncio.sleep(self.settings.cache_warmup_delay)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive; report it in devo_check
                log.exception("cache warm-up failed")
                self.status.update(state="failed", last_result="error", last_message=repr(e)[:300])
            hours = self.settings.cache_check_hours
            if hours <= 0:
                self.status["state"] = self.status["state"] if self.status["state"] == "failed" else "idle"
                return
            self.status["next_run_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))
            await asyncio.sleep(hours * 3600)

    def describe(self) -> dict:
        return json.loads(json.dumps(self.status))
