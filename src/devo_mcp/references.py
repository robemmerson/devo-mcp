"""The skill's reference library (LINQ, APIs, table guides, playbooks, detections), served
read-only to clients as MCP resources and through the devo_reference tool (for clients that
don't read resources, such as most workflow tools)."""
from __future__ import annotations

from pathlib import Path

from .engine_runner import REFERENCES

# One line per file, shown when a client lists the references.
DESCRIPTIONS = {
    "commands.md": "Every option of the helper commands behind the tools: chunking, auto-split, merging, batch specs, sorting",
    "playbooks.md": "Question playbooks: logons, privileged grants, health, credential attacks, coverage, activity, Teams",
    "linq-traps.md": "Every known LINQ trap with fixes: read before non-trivial queries and whenever a query returns 0 rows or fails",
    "linq-syntax.md": "LINQ syntax: clauses, subqueries, time, lookups, performance",
    "linq-functions.md": "LINQ function reference (grep by name or purpose)",
    "table-guide.md": "How to choose a table; entity-resolution cheat-sheet; event time per family (index of table-guide/)",
    "investigations.md": "Alert triage and entity investigation workflow, pivot queries, per-alert checklists, PIM recipe, report template",
    "dashboards-api.md": "Activeboards: endpoints, board JSON, widget types, inputs, sharing",
    "alerts-api.md": "Alert fields, statuses and endpoints in depth",
    "query-api.md": "Query API details, error codes, response modes",
    "tables.md": "Devo's general table catalogue, union tables and normalised fields",
    "table-catalogue.md": "Every table Devo can parse",
    "cross-tool-sentinelone.md": "Upload/file-sharing questions needing SentinelOne PowerQuery alongside Devo",
    "library/INDEX.md": "Index of ~580 Devo SecOps detection queries (name | file | tables | MITRE)",
}
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".linq"}
MAX_GREP = 200  # characters in a client grep


def all_files() -> list[str]:
    return sorted(p.relative_to(REFERENCES).as_posix() for p in REFERENCES.rglob("*")
                  if p.is_file() and p.suffix in TEXT_SUFFIXES)


def resolve(name: str) -> Path:
    name = name.strip().lstrip("/").removeprefix("references/")
    p = (REFERENCES / name).resolve()
    if REFERENCES.resolve() not in p.parents or not p.is_file():
        matches = [f for f in all_files() if f.endswith("/" + name) or f == name or Path(f).stem == name]
        if len(matches) == 1:
            return REFERENCES / matches[0]
        raise ValueError(f"no reference named {name!r}; call devo_reference without a name to list them")
    return p


def catalogue() -> list[dict]:
    out = []
    for f in all_files():
        p = REFERENCES / f
        out.append({"name": f, "lines": sum(1 for _ in p.open(encoding="utf-8", errors="replace")),
                    "about": DESCRIPTIONS.get(f, _first_heading(p))})
    return out


def _first_heading(p: Path) -> str:
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    return line.lstrip("# ").strip()
    except OSError:
        pass
    return ""


def read(name: str, grep: str | None = None, offset: int = 0, limit: int = 400, context: int = 2) -> dict:
    p = resolve(name)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    rel = p.relative_to(REFERENCES).as_posix()
    if grep:
        # Plain terms, not a regex: a client regex could backtrack for ever, and Python can't
        # interrupt it.
        if len(grep) > MAX_GREP:
            raise ValueError(f"grep longer than {MAX_GREP} characters")
        terms = [t.strip().casefold() for t in grep.split("|") if t.strip()]
        if not terms:
            raise ValueError("grep needs at least one term")
        keep: set[int] = set()
        for i, line in enumerate(lines):
            low = line.casefold()
            if any(t in low for t in terms):
                keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
        picked = sorted(keep)
        shown = picked[offset:offset + limit]
        out, prev = [], None
        for i in shown:
            if prev is not None and i != prev + 1:
                out.append("--")
            out.append(f"{i + 1}: {lines[i]}")
            prev = i
        nxt = offset + len(shown)
        return {"name": rel, "grep": grep, "matched_lines": len(picked), "text": "\n".join(out),
                "next_offset": nxt if nxt < len(picked) else None}
    chunk = lines[offset:offset + limit]
    nxt = offset + len(chunk)
    return {"name": rel, "offset": offset, "total_lines": len(lines), "text": "\n".join(chunk),
            "next_offset": nxt if nxt < len(lines) else None}


def bundled_specs() -> dict[str, Path]:
    """Batch specs shipped with the skill, by stem (e.g. credential-attacks)."""
    return {p.stem: p for p in sorted((REFERENCES / "specs").glob("*.jsonl"))}
