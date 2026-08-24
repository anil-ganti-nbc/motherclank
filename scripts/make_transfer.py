#!/usr/bin/env python3
"""Mechanical transfer-artifact generator (§12 hardening).

Derives base/head SHAs, commit counts, branch names and bundle paths from
git itself - never from hand-maintained prose. Emits bundles, patch
fallbacks, and a manifest.json; then verifies each bundle with
``git bundle verify``.

Usage:
    python make_transfer.py --repo . --branch f6-continuity-f2 \
        --out ../clank-transfer [--base <sha>]

Base resolution: explicit --base, else merge-base of origin/--base-origin
and --branch.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def safe_branch_name(branch: str) -> str:
    return branch.replace("/", "-")


def git(repo: Path, *args: str) -> str:
    out = _run(["git", "-C", str(repo), *args])
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # One bounded retry for the intermittent Windows "WinError 6: handle is
    # invalid" race when spawning under captured stdio (seen with pytest).
    last_exc: OSError | None = None
    for _ in range(2):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:  # pragma: no cover - platform-specific race
            last_exc = exc
    raise SystemExit(f"subprocess failed repeatedly ({last_exc}): {cmd[0]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--base-origin", default="main")
    ap.add_argument("--base", default=None,
                    help="explicit base SHA; overrides --base-origin lookup")
    ap.add_argument("--out", required=True, type=Path)
    # Transfer-chain hardening: sequenced names prevent a later phase from
    # silently overwriting a prerequisite phase's bundle (the exact failure
    # that made P-4.1 unreachable). Both --seq and --label are REQUIRED
    # together and become part of the artifact name.
    ap.add_argument("--seq", default=None,
                    help="chain step number, e.g. 01; enables sequenced naming")
    ap.add_argument("--label", default=None,
                    help="increment label, e.g. p41 or fgt-capability")
    args = ap.parse_args()
    if bool(args.seq) != bool(args.label):
        raise SystemExit("--seq and --label must be used together")

    repo = args.repo.resolve()
    branch = args.branch
    head = git(repo, "rev-parse", branch)
    if args.base:
        base = git(repo, "rev-parse", args.base)
    else:
        base = git(repo, "merge-base", f"origin/{args.base_origin}", branch)
    subjects = git(repo, "log", "--format=%h %s", f"{base}..{branch}").splitlines()

    if args.seq:
        bundle = args.out / f"{args.seq}-{repo.name}-{args.label}.bundle"
        patch_dir = args.out / f"patches-{args.seq}-{args.label}"
    else:
        bundle = args.out / f"{repo.name}-{safe_branch_name(branch)}.bundle"
        patch_dir = args.out / f"patches-{repo.name}-{safe_branch_name(branch)}"
    args.out.mkdir(parents=True, exist_ok=True)

    if bundle.exists():
        raise SystemExit(
            f"refusing to overwrite existing bundle: {bundle}. "
            "Bundles are immutable evidence; use a new seq/label.")

    git(repo, "bundle", "create", str(bundle), f"{base}..{branch}")
    if patch_dir.exists():
        for p in patch_dir.iterdir():
            p.unlink()
    else:
        patch_dir.mkdir(parents=True)
    git(repo, "format-patch", f"{base}..{branch}",
        f"--output-directory={patch_dir}", "--quiet")

    # independent verification - trust nothing, including our own write.
    # Verify inside the source repo (it contains the base commit).
    verify = _run(["git", "-C", str(repo), "bundle", "verify", str(bundle)])
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": repo.name,
        "branch": branch,
        "seq": args.seq,
        "label": args.label,
        "base_sha": base,
        "head_sha": head,
        "commit_count": len(subjects),
        "commits": subjects,
        "bundle": bundle.name,
        "patches_dir": patch_dir.name,
        "bundle_verified": verify.returncode == 0,
        "requires_base_on_remote": f"origin/{args.base_origin} must contain {base}",
    }
    manifest_path = args.out / (
        f"manifest-{args.seq}-{repo.name}.json" if args.seq
        else f"manifest-{repo.name}-{safe_branch_name(branch)}.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0 if verify.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
