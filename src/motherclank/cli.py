"""Motherclank M0 CLI.

    motherclank harvest --inventory fleet.yaml --real-state DIR [--out DIR] [--dry-run]

Read-only by construction: the only files written are Motherclank's own
snapshot/report outputs. --dry-run prints the report and the snapshot payload
without touching disk beyond stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import AdapterPlaneUnavailable, build_adapters
from . import snapshot as snap
from .report import render_report, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="motherclank")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest", help="read-only fleet observation snapshot")
    h.add_argument("--inventory", required=True, type=Path, help="path to fleet.yaml")
    h.add_argument("--real-state", required=True, type=Path,
                   help="directory holding read-only DB copies (watch_clank.db, ...)")
    h.add_argument("--adapters-src", type=Path, default=None,
                   help="path to diagnostic-clank checkout if not a workspace sibling")
    h.add_argument("--out", type=Path, default=Path("var"), help="output directory")
    h.add_argument("--dry-run", action="store_true",
                   help="compute and print; write nothing")
    args = parser.parse_args(argv)

    if args.command == "harvest":
        return _harvest(args)
    return 2


def _harvest(args) -> int:
    if not args.inventory.exists():
        print(f"inventory missing: {args.inventory}", file=sys.stderr)
        return 3
    try:
        built = build_adapters(args.real_state)
    except AdapterPlaneUnavailable as exc:
        print(f"adapter plane unavailable: {exc}", file=sys.stderr)
        return 4

    payload, warnings = snap.build_snapshot(
        inventory_path=args.inventory,
        adapters_result=built,
        real_state_dir=args.real_state,
        out_dir=args.out,
    )

    if args.dry_run:
        print(render_report(payload))
        print("--- snapshot payload ---")
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    else:
        target = snap.append_snapshot(args.out, payload)
        report = write_report(args.out, payload)
        print(f"snapshot: {target}")
        print(f"report:   {report}")

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
