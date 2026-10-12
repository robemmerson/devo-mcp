#!/usr/bin/env bash
# Copy the Devo skill (helper + references) from a secops-skills checkout into the package.
# Usage: scripts/sync-engine.sh [path/to/secops-skills]   (default ../secops-skills)
set -euo pipefail
src_repo="${1:-$(dirname "$0")/../../secops-skills}"
src="$src_repo/plugins/devo/skills/devo"
dest="$(cd "$(dirname "$0")/.." && pwd)/src/devo_mcp/engine"
[ -f "$src/scripts/devo.py" ] || { echo "no Devo skill at $src" >&2; exit 1; }
rm -rf "$dest"
mkdir -p "$dest"
cp -R "$src/SKILL.md" "$src/scripts" "$src/references" "$dest/"
find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} +
commit="$(git -C "$src_repo" rev-parse HEAD 2>/dev/null || echo unknown)"
dirty="$(git -C "$src_repo" status --porcelain -- plugins/devo 2>/dev/null | head -1)"
cat > "$dest/VENDORED.json" <<JSON
{
  "source": "secops-skills/plugins/devo/skills/devo",
  "commit": "$commit",
  "dirty": $([ -n "$dirty" ] && echo true || echo false),
  "synced": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "engine synced from $src_repo @ ${commit:0:12}"
