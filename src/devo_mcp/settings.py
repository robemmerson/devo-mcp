"""Server settings, read once from the environment.

The Devo credentials themselves (DEVO_TOKEN, DEVO_REGION, DEVO_CACHE_KEY, DEVO_ENV_FILE) are not
read here: they pass straight through to the engine, which owns their parsing.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool = False, env=None) -> bool:
    v = (env if env is not None else os.environ).get(name)
    if v is None or v.strip() == "":
        return default
    if v.strip().lower() not in TRUE | FALSE:  # a typo must not silently flip a security setting
        raise SystemExit(f"{name} must be true or false, got {v!r}")
    return v.strip().lower() in TRUE


def env_int(name: str, default: int, env=None) -> int:
    v = (env if env is not None else os.environ).get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {v!r}")


def default_workdir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    if not os.path.isdir(os.path.dirname(base)):
        base = tempfile.gettempdir()
    return Path(base) / "devo-mcp" / "work"


def token_warning(env=None) -> str | None:
    """A startup warning when the engine will find no Devo token (it reports this per call otherwise)."""
    env = os.environ if env is None else env
    if env.get("DEVO_TOKEN"):
        return None
    path = os.path.expanduser(env.get("DEVO_ENV_FILE") or os.path.join("~", ".config", "devo", "env"))
    if not os.access(path, os.R_OK):
        return (f"warning: DEVO_TOKEN is not set and {path} is missing or unreadable by this user "
                f"(uid {os.getuid() if hasattr(os, 'getuid') else '?'}): every Devo call will fail")
    return None


def cache_profile(v: str) -> str:
    v = (v or "all").strip().lower()
    if v not in ("all", "none") and not v.isdigit():
        raise SystemExit(f"DEVO_MCP_CACHE_PROFILE must be all, none or a number of tables, got {v!r}")
    return v


@dataclass(frozen=True)
class Settings:
    allow_writes: bool = False
    workdir: Path = field(default_factory=default_workdir)
    work_ttl_hours: int = 48
    max_output_chars: int = 40_000
    max_concurrency: int = 4
    max_call_seconds: int = 3600
    max_job_seconds: int = 6 * 3600
    soft_timeout: int = 45
    python: str = sys.executable
    api_key: str | None = None
    allow_no_auth: bool = False
    allowed_hosts: tuple[str, ...] = ()
    cache_warmup: bool = False
    cache_profile: str = "all"
    cache_check_hours: int = 24
    cache_parallel: int = 6
    cache_warmup_delay: float = 10.0
    tool_errors_as_text: bool = False

    def __post_init__(self):
        if self.soft_timeout < 0:
            raise SystemExit("DEVO_MCP_SOFT_TIMEOUT_SECONDS can't be negative (0 = wait as long as it takes)")
        if 0 < self.work_ttl_hours * 3600 <= self.max_job_seconds:
            raise SystemExit("DEVO_MCP_WORK_TTL_HOURS must be longer than DEVO_MCP_MAX_JOB_SECONDS, or a "
                             "running job's directory could be deleted under it")

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        workdir = env.get("DEVO_MCP_WORKDIR")
        hosts = tuple(h.strip() for h in env.get("DEVO_MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
        return cls(
            allow_writes=env_bool("DEVO_MCP_ALLOW_WRITES", env=env),
            workdir=Path(workdir).expanduser() if workdir else default_workdir(),
            work_ttl_hours=env_int("DEVO_MCP_WORK_TTL_HOURS", 48, env),
            max_output_chars=env_int("DEVO_MCP_MAX_OUTPUT_CHARS", 40_000, env),
            max_concurrency=max(1, env_int("DEVO_MCP_MAX_CONCURRENCY", 4, env)),
            max_call_seconds=env_int("DEVO_MCP_MAX_CALL_SECONDS", 3600, env),
            max_job_seconds=env_int("DEVO_MCP_MAX_JOB_SECONDS", 6 * 3600, env),
            soft_timeout=env_int("DEVO_MCP_SOFT_TIMEOUT_SECONDS", 45, env),
            api_key=(env.get("MCP_API_KEY") or "").strip() or None,
            allow_no_auth=env_bool("DEVO_MCP_ALLOW_NO_AUTH", env=env),
            allowed_hosts=hosts,
            cache_warmup=env_bool("DEVO_MCP_CACHE_WARMUP", True, env),
            cache_profile=cache_profile(env.get("DEVO_MCP_CACHE_PROFILE", "all")),
            cache_check_hours=env_int("DEVO_MCP_CACHE_CHECK_HOURS", 24, env),
            cache_parallel=max(1, env_int("DEVO_MCP_CACHE_PARALLEL", 6, env)),
            tool_errors_as_text=env_bool("MCP_TOOL_ERRORS_AS_TEXT", env=env),
        )
