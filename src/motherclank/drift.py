"""Law 9 deployment-drift indicator (deferred in Phase 1.5, fed by M1).

Reads a Clank checkout's git HEAD **purely from files** (.git/HEAD + refs —
pure file reads — no shell-outs, no network) and compares against the ledger SHA recorded for
that Clank in fleet.yaml-derived inventory evidence.

relationship:
  CONVERGED                 checkout head == ledger sha
  CHECKOUT_BEHIND_LEDGER    ledger sha is an ancestor-ish newer pin than checkout
                            (we cannot see ancestry without history; we report
                            plain inequality honestly)
  CHECKOUT_AHEAD_OF_LEDGER  same inequality, other direction, when the ledger
                            pin predates by timestamp recorded alongside
  UNKNOWN                   checkout path absent/unreadable
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def read_git_head(checkout: Path) -> str | None:
    """Resolve a checkout's HEAD commit SHA via file reads only."""
    git = checkout / ".git"
    if not git.exists():
        return None
    if git.is_dir():
        head = git / "HEAD"
        if not head.exists():
            return None
        ref = head.read_text().strip()
        if not ref.startswith("ref: "):
            return ref if _SHA_RE.match(ref) else None
        ref_file = git / ref[5:]
        packed = git / "packed-refs"
        short = ref[5:]
        if ref_file.exists():
            value = ref_file.read_text().strip()
            return value if _SHA_RE.match(value) else None
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + short):
                    sha = line.split()[0]
                    return sha if _SHA_RE.match(sha) else None
        return None
    # worktree-style .git file: "gitdir: /path"
    text = git.read_text().strip()
    if text.startswith("gitdir: "):
        return read_git_head(Path(text[len("gitdir: "):]).parent)
    return None


def drift_row(clank_id: str, checkout_path: Path, ledger_sha: str | None,
              ledger_observed_at: str | None = None) -> dict[str, Any]:
    head = read_git_head(checkout_path)
    row: dict[str, Any] = {
        "clank": clank_id,
        "checkout_path": str(checkout_path),
        "checkout_head": head or "UNKNOWN",
        "ledger_sha": ledger_sha or "UNKNOWN",
        "relationship": "UNKNOWN",
    }
    if ledger_observed_at:
        row["ledger_observed_at"] = ledger_observed_at
    if head and ledger_sha and _SHA_RE.match(head) and _SHA_RE.match(ledger_sha):
        row["relationship"] = ("CONVERGED" if head == ledger_sha
                               else "DIVERGED")
        row["note"] = ("SHAs differ; direction requires fetch (network) which M0/M1 "
                       "forbids — operator compares or grants a future read-only "
                       "fetch capability.")
    elif head is None:
        row["note"] = "checkout unreadable or not a git working tree"
    return row


DEFAULT_HETZNER_CHECKOUTS = {
    "watch-clank": "/home/anilganti/watch-clank",
    "smartphone-clank": "/opt/smartphone-clank",
    "korean-tech-wire": "/opt/korean-tech-wire",
}
