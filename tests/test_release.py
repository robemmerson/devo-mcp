import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("release", Path(__file__).parent.parent / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_classify():
    c = release.classify
    assert c("feat: x", "") == ("feat", "minor")
    assert c("fix(http): x", "") == ("fix", "patch")
    assert c("feat!: x", "") == ("breaking", "major")
    assert c("refactor: x", "BREAKING CHANGE: y")[1] == "major"
    assert c("chore(deps): bump mcp", "")[1] == "patch"
    assert c("docs: x", "")[1] is None and c("ci: x", "")[1] is None and c("chore: x", "")[1] is None
    assert c("Merge pull request #3", "")[1] == "patch"


def test_bump():
    assert release.bump((0, 1, 0), "major") == (0, 2, 0)  # 0.x: breaking bumps minor
    assert release.bump((1, 2, 3), "major") == (2, 0, 0)
    assert release.bump((1, 2, 3), "minor") == (1, 3, 0)
    assert release.bump((1, 2, 3), "patch") == (1, 2, 4)
