"""Server-side scratch space for files the engine reads and writes (query exports, activity
sweeps, batch outputs, board files). Tools refer to these files by paths relative to the root,
so a client never names a path on the server's disk."""
from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path, ttl_hours: int = 48):
        self.root = Path(root).expanduser().resolve()
        self.ttl = ttl_hours * 3600
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def new_dir(self, kind: str) -> Path:
        """A fresh directory named <kind>-<UTC time>-<random>, so names sort by age."""
        self.cleanup()
        name = f"{kind}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"
        d = self.root / name
        d.mkdir(mode=0o700)
        return d

    def resolve(self, rel: str, must_exist: bool = True) -> Path:
        """A client-supplied relative path, refused if it escapes the workspace."""
        if not rel or not isinstance(rel, str):
            raise WorkspaceError("empty workspace path")
        rel = rel.strip().lstrip("/")
        if "\x00" in rel:
            raise WorkspaceError("workspace paths can't contain NUL")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise WorkspaceError(f"{rel!r} is outside the workspace")
        if must_exist and not p.exists():
            raise WorkspaceError(f"{rel!r} does not exist in the workspace (see devo_workspace_list)")
        return p

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def listing(self, rel: str | None = None) -> list[dict]:
        base = self.resolve(rel) if rel else self.root
        if base.is_file():
            base = base.parent
        out = []
        for p in sorted(base.iterdir()):
            st = p.stat()
            out.append({"path": self.rel(p), "type": "dir" if p.is_dir() else "file",
                        "bytes": st.st_size if p.is_file() else None,
                        "modified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))})
        return out

    def read_lines(self, rel: str, offset: int = 0, limit: int = 200, max_chars: int = 40_000) -> dict:
        p = self.resolve(rel)
        if p.is_dir():
            raise WorkspaceError(f"{rel!r} is a directory; use devo_workspace_list")
        lines, total, used, truncated = [], 0, 0, False
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                total = i + 1
                if i < offset or len(lines) >= limit or truncated:
                    continue
                line = line.rstrip("\n")
                if used + len(line) > max_chars:
                    if not lines:  # one huge line: return its head rather than nothing
                        lines.append(line[:max_chars])
                    truncated = True
                    continue
                lines.append(line)
                used += len(line) + 1
        nxt = offset + len(lines)
        return {"path": self.rel(p), "offset": offset, "lines": lines, "total_lines": total,
                "next_offset": nxt if nxt < total else None, "truncated_by_size": truncated}

    def cleanup(self) -> None:
        """Drop top-level entries older than the TTL."""
        if self.ttl <= 0:
            return
        cutoff = time.time() - self.ttl
        for p in self.root.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass
