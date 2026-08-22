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
from . import synthesis as syn
from .drift import drift_row, DEFAULT_HETZNER_CHECKOUTS
from .report import render_report, write_report, render_synthesis, render_anomalies, render_recommendations
from . import anomalies as ano
from . import recommendations as recs


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
    z = sub.add_parser("synthesize", help="derive fleet health from the latest M0 snapshot")
    z.add_argument("--var-dir", required=True, type=Path,
                   help="M0 output directory containing snapshots/")
    z.add_argument("--out", type=Path, default=Path("var"))
    z.add_argument("--stale-hours", type=float, default=24.0)
    z.add_argument("--drift-checkouts", type=Path, default=None,
                   help="optional JSON {clank: checkout_path} for Law 9 metric")
    z.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("detect", help="deterministic anomaly ledger from M0/M1 history")
    d.add_argument("--var-dir", required=True, type=Path)
    d.add_argument("--out", type=Path, default=Path("var"))
    d.add_argument("--dry-run", action="store_true")
    rr = sub.add_parser("recommend", help="advisory operator recommendations from the anomaly ledger")
    rr.add_argument("--var-dir", required=True, type=Path)
    rr.add_argument("--out", type=Path, default=Path("var"))
    rr.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "harvest":
        return _harvest(args)
    if args.command == "synthesize":
        return _synthesize(args)
    if args.command == "detect":
        return _detect(args)
    if args.command == "recommend":
        return _recommend(args)
    return 2


def _recommend(args) -> int:
    batch = recs.read_latest_anomaly_batch(args.var_dir)
    if batch is None:
        print(f"no anomaly batches under {args.var_dir / 'anomalies'}", file=sys.stderr)
        return 6
    recs_list = recs.derive_recommendations(batch)
    payload = recs.build_batch(args.out, batch, recs_list)
    if args.dry_run:
        print(render_recommendations(payload))
        print("--- recommendations payload ---")
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    else:
        target = recs.append_batch(args.out, payload)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"recommendations-{payload['generated_from'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_recommendations(payload))
        print(f"recommendations: {target}")
        print(f"report:          {report}")
        print(f"active={payload['active_count']} closed={payload['closed_count']}")
    return 0

    if args.command == "harvest":
        return _harvest(args)
    if args.command == "synthesize":
        return _synthesize(args)
    if args.command == "detect":
        return _detect(args)
    return 2


def _detect(args) -> int:
    history = ano.load_history(args.var_dir)
    if not history:
        print(f"no snapshots under {args.var_dir / 'snapshots'}", file=sys.stderr)
        return 5
    found = ano.detect(history)
    batch = ano.build_batch(args.out, history, found)
    if args.dry_run:
        print(render_anomalies(batch))
        print("--- anomaly batch payload ---")
        print(json.dumps(batch, sort_keys=True, indent=2, default=str))
    else:
        target = ano.append_batch(args.out, batch)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"anomalies-{batch['batch_generated_from'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_anomalies(batch))
        print(f"anomalies: {target}")
        print(f"report:    {report}")
        print(f"active={batch['active_count']} recovered={batch['recovered_count']}")
    return 0


def _synthesize(args) -> int:
    payload = syn.read_latest_snapshot(args.var_dir)
    if payload is None:
        print(f"no snapshots found under {args.var_dir / 'snapshots'}", file=sys.stderr)
        return 5
    synthesis = syn.synthesize_fleet(payload, stale_hours=args.stale_hours)
    drift_rows = []
    if args.drift_checkouts:
        mapping = json.loads(args.drift_checkouts.read_text())
        inventory = json.loads(json.dumps({}))  # placeholder; ledger SHAs come from var inventory copy if present
        ledger = payload.get("inventory_ledger") or {}
        observed_at = payload.get("harvested_at_utc")
        for cid, checkout in mapping.items():
            drift_rows.append(drift_row(cid, Path(checkout),
                                        ledger.get(cid),
                                        observed_at))
    syn.attach_law9_drift(synthesis, drift_rows)
    synthesis["content_hash"] = syn.content_hash(synthesis)
    prev = syn.previous_synthesis_hash(args.out)
    synthesis["previous_synthesis_hash"] = prev

    if args.dry_run:
        print(render_synthesis(synthesis))
        print("--- synthesis payload ---")
        print(json.dumps(synthesis, sort_keys=True, indent=2, default=str))
    else:
        target = syn.append_synthesis(args.out, synthesis)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"fleet-synthesis-{synthesis['synthesized_at_utc'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_synthesis(synthesis))
        print(f"synthesis: {target}")
        print(f"report:    {report}")
    return 0


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
