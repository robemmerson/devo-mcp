#!/usr/bin/env python3
"""Automatic semver from Conventional Commits. Stdlib only.

The next version comes from the commits since the last `vX.Y.Z` tag:

  feat!: … / BREAKING CHANGE:                     -> major
  feat: …                                         -> minor
  fix:, perf:, refactor:, revert:, build:, deps   -> patch
  docs:, ci:, test:, chore:, style: only          -> no release

With no tag yet, the first release is the version in pyproject.toml.

  python3 scripts/release.py plan              # JSON: {"release": bool, "version", "tag", "bump", "notes"}
  python3 scripts/release.py stamp X.Y.Z       # write X.Y.Z into pyproject.toml and __init__.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "devo_mcp" / "__init__.py"
CONVENTIONAL = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<subject>.+)$")
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
RELEASING = {"feat", "fix", "perf", "refactor", "revert", "build"}
SECTIONS = [("Breaking changes", "breaking"), ("Features", "feat"), ("Fixes", "fix"), ("Other changes", "other")]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def semver(s: str):
    m = SEMVER.match(s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def last_tag() -> str | None:
    tags = [t for t in git("tag", "--list", "v*").split() if semver(t)]
    return max(tags, key=semver) if tags else None


def commits_since(tag: str | None) -> list[tuple[str, str, str]]:
    # --no-merges: a "Merge pull request" commit isn't a change of its own (the commits it brings are)
    out = git("log", "--no-merges", f"{tag}..HEAD" if tag else "HEAD", "--format=%H%x1f%s%x1f%b%x1e")
    commits = []
    for rec in out.split("\x1e"):
        if rec.strip():
            sha, subject, body = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
            if not subject.startswith("chore(release)"):
                commits.append((sha, subject, body))
    return commits


def classify(subject: str, body: str) -> tuple[str, str | None]:
    """(section, bump) for one commit; bump None = doesn't trigger a release."""
    m = CONVENTIONAL.match(subject)
    if (m and m.group("bang")) or "BREAKING CHANGE" in body:
        return "breaking", "major"
    if not m:
        return "other", "patch"  # not conventional: release it rather than lose it
    typ, scope = m.group("type").lower(), (m.group("scope") or "").lower()
    if typ == "feat":
        return "feat", "minor"
    if typ == "fix":
        return "fix", "patch"
    if typ in RELEASING or scope == "deps":
        return "other", "patch"
    return "other", None


def bump(version: tuple[int, int, int], kind: str) -> tuple[int, int, int]:
    major, minor, patch = version
    if kind == "major":
        return (major + 1, 0, 0) if major else (0, minor + 1, 0)  # 0.x: breaking bumps minor
    if kind == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def base_version() -> tuple[int, int, int]:
    m = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(), re.M)
    v = semver(m.group(1)) if m else None
    if not v:
        sys.exit("pyproject.toml has no X.Y.Z version")
    return v


def plan() -> dict:
    tag = last_tag()
    commits = commits_since(tag)
    kinds, sections = [], {k: [] for _, k in SECTIONS}
    for sha, subject, body in commits:
        section, kind = classify(subject, body)
        if kind:
            kinds.append(kind)
        sections[section].append(f"- {subject} ({sha[:7]})")
    if not kinds:
        return {"release": False, "previous": tag, "reason": "no releasable commits since " + (tag or "the start")}
    kind = next(k for k in ("major", "minor", "patch") if k in kinds)
    version = bump(semver(tag), kind) if tag else base_version()
    v = ".".join(map(str, version))
    notes = "\n\n".join(f"### {title}\n" + "\n".join(sections[key]) for title, key in SECTIONS if sections[key])
    return {"release": True, "version": v, "tag": f"v{v}", "bump": kind if tag else "initial", "previous": tag,
            "notes": notes}


def stamp(version: str) -> None:
    if not semver(version):
        sys.exit(f"not a version: {version}")
    v = version.lstrip("v")
    PYPROJECT.write_text(re.sub(r'^version = "[^"]+"', f'version = "{v}"', PYPROJECT.read_text(), count=1, flags=re.M))
    INIT.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{v}"', INIT.read_text(), count=1))


def main(argv: list[str]) -> int:
    if argv[:1] == ["plan"]:
        print(json.dumps(plan(), indent=1))
    elif argv[:1] == ["stamp"] and len(argv) == 2:
        stamp(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
