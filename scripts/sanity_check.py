#!/usr/bin/env python3
"""Operator sanity check - reports fleet observer health at a glance."""
import subprocess
import sys
from pathlib import Path

MC = Path(__file__).resolve().parent

def git(*args):
    r = subprocess.run(["git", "-C", str(MC), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "N/A"

print("=== MOTHERCLANK OBSERVER SANITY CHECK ===")
print(f"checkout HEAD (not a deployed SHA): {git('rev-parse', '--short', 'HEAD')}")
print(f"origin/main HEAD (not a deployed SHA): {git('rev-parse', '--short', 'origin/main')}")

sys.path.insert(0, str(MC / "src"))

from motherclank.adapters import load_registry
registry = load_registry(None)
print(f"registered lanes: {len(registry)}")
for cid in sorted(registry):
    print(f"  {cid}")

from motherclank.golden_corpus import ids as gic_ids
print(f"formal GIC count: {len(gic_ids())}")

snap_dir = MC / "var" / "snapshots"
if snap_dir.exists():
    newest = sorted(snap_dir.glob("*.jsonl"))[-1]
    failed = sum(1 for line in newest.read_text().strip().split("\n")
                 if line.strip() and "FAILED_ADAPTER" in line)
    print(f"latest snapshot FAILED_ADAPTER count: {failed}")

print("\n=== BOUNDED DEBT (open) ===")
debts = [
    ("ACT-003 governance sign-off", "GOVERNANCE"),
    ("durable off-host redundancy", "INFRASTRUCTURE"),
    ("SourceHealthEntry layer dimension", "BOUNDED_FUTURE"),
    ("report.py clock display", "COSMETIC"),
]
for item, cls in debts:
    print(f"  [{cls}] {item}")

print("\n=== ARCHITECTURE FREEZE ===")
print("v0.3 FROZEN until real incident or participant proves contract insufficient.")
