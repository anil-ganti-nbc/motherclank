#!/usr/bin/env python3
"""verify_transfer.py - mechanically prove a transfer chain is consumable.

Given a FRESH CLONE of canonical GitHub and an ordered list of transfer
manifests, this tool fails loudly if any prerequisite object is missing,
any bundle fails verification, any fetched head mismatches its manifest,
or any base stops being reachable from the previous step.

This exists because a handoff once shipped bundles whose prerequisite
commits lived only on an ephemeral agent machine. Never again.

Usage:
    python verify_transfer.py --clone /path/to/fresh-clone \
        --manifest 01-x.json --manifest 02-y.json [--canonical-ref origin/main]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, close_fds=True, **kw)


def verify_chain(clone: Path, manifests: list[Path],
                 canonical_ref: str) -> tuple[int, list[str]]:
    log: list[str] = []
    ok = True
    prev_heads: dict[str, str] = {}

    for mf in manifests:
        m = json.loads(mf.read_text(encoding="utf-8"))
        repo = m["repo"]
        seq = m.get("seq", "?")
        label = m.get("label", "")
        step = f"[{seq}/{repo}/{label or 'unlabeled'}]"
        bundle = mf.parent / m["bundle"]

        # The clone must be for the right repo.
        origin_url = _run(["git", "-C", str(clone), "remote", "get-url",
                           "origin"]).stdout.strip()
        if repo not in origin_url:
            print(f"FAIL {step}: clone at {clone} is not {repo} "
                  f"(origin={origin_url})", file=sys.stderr)
            return 1, log

        # 1. bundle verification INSIDE the fresh clone - proves every
        #    prerequisite object is present.
        v = _run(["git", "-C", str(clone), "bundle", "verify", str(bundle)])
        if v.returncode != 0:
            print(f"FAIL {step}: bundle verify failed:\n{v.stderr}",
                  file=sys.stderr)
            return 1, log
        log.append(f"{step} bundle verified")

        # 2. fetch into a throwaway ref.
        ref = f"refs/transfers/{seq}-{label or repo}"
        f = _run(["git", "-C", str(clone), "fetch", str(bundle),
                  f"{m['branch']}:{ref}"])
        if f.returncode != 0:
            print(f"FAIL {step}: fetch failed:\n{f.stderr}", file=sys.stderr)
            return 1, log

        # 3. head must match manifest exactly.
        head = _run(["git", "-C", str(clone), "rev-parse", ref]).stdout.strip()
        if head != m["head_sha"]:
            print(f"FAIL {step}: fetched head {head[:12]} != manifest "
                  f"{m['head_sha'][:12]}", file=sys.stderr)
            return 1, log

        # 4. base must be reachable: either from canonical ref (first step)
        #    or from the previous accepted step's head for this repo.
        anchor = prev_heads.get(repo) or canonical_ref
        anc = _run(["git", "-C", str(clone), "merge-base", "--is-ancestor",
                    m["base_sha"], anchor])
        if anc.returncode != 0:
            print(f"FAIL {step}: declared base {m['base_sha'][:12]} is not "
                  f"reachable from {anchor}", file=sys.stderr)
            return 1, log

        # 5. commit count sanity vs manifest.
        n = len(_run(["git", "-C", str(clone), "log", "--format=%h",
                      f"{m['base_sha']}..{ref}"]).stdout.splitlines())
        if n != m["commit_count"]:
            print(f"FAIL {step}: commit count {n} != manifest "
                  f"{m['commit_count']}", file=sys.stderr)
            return 1, log

        prev_heads[repo] = head
        log.append(f"{step} head {head[:12]} reached from "
                   f"{m['base_sha'][:12]} ({n} commits)")

    return (0 if ok else 1), log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clone", required=True, type=Path,
                    help="fresh clone of canonical GitHub")
    ap.add_argument("--manifest", required=True, action="append", type=Path,
                    help="ordered transfer manifests; repeat per step")
    ap.add_argument("--canonical-ref", default="origin/main")
    args = ap.parse_args()

    rc, log = verify_chain(args.clone.resolve(), args.manifest,
                           args.canonical_ref)
    for line in log:
        print("OK ", line)
    print("TRANSFER CHAIN:", "CONSUMABLE" if rc == 0 else "BROKEN")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
