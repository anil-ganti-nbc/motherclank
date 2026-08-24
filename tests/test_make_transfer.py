"""make_transfer.py: handoff metadata must be derived from git, never prose.

Builds a synthetic two-commit repo, generates transfer artifacts with an
explicit base, and asserts the manifest matches the real commit graph.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SCRIPT = HERE / "scripts" / "make_transfer.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_transfer", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, close_fds=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _init_repo(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "-q", "--allow-empty", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "-q", "--allow-empty", "-m", "work one")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "-q", "--allow-empty", "-m", "work two")
    head = _git(path, "rev-parse", "HEAD")
    return base, head


def test_manifest_matches_real_commit_graph(tmp_path):
    mt = _load()
    repo = tmp_path / "synthetic"
    base, head = _init_repo(repo)
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    out = tmp_path / "transfer"

    argv_backup = __import__("sys").argv
    __import__("sys").argv = [
        "make_transfer.py", "--repo", str(repo),
        "--branch", branch, "--base", base, "--out", str(out)]
    try:
        rc = mt.main()
    finally:
        __import__("sys").argv = argv_backup
    assert rc == 0

    manifest = json.loads(
        (out / f"manifest-{branch}.json").read_text("utf-8"))
    # every metadata value comes from git, and matches it exactly:
    assert manifest["base_sha"] == base
    assert manifest["head_sha"] == head
    assert manifest["commit_count"] == 2
    assert len(manifest["commits"]) == 2
    assert manifest["bundle_verified"] is True
    assert list((out / f"patches-{branch}").glob("*.patch"))
