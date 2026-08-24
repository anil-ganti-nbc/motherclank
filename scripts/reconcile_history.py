#!/usr/bin/env python3
"""H-4 — offline Motherclank var/ history reconciler (READ-ONLY).

Given an exported Motherclank ``var/`` tree and an incident window, map
every artifact record intersecting the window to its identity, derived
claim, and post-hoc continuity/liveness qualification. Never writes.

Usage:
    python reconcile_history.py --var-dir EXPORTED_VAR \
        --start 2026-08-22T09:00:00Z --end 2026-08-24T00:00:00Z \
        [--clank feature-phone-clank] [--out report.json]

Artifact classes and their timestamp keys:
    snapshots        harvested_at_utc
    syntheses        synthesized_at_utc (joined via snapshot_hash)
    anomalies        batch_generated_from (batch) + per-record first/last_seen
    recommendations  generated_from
    qc_corpus        generated_from
    soak             window latest

Continuity/liveness qualification is re-derived from the registries present
in the same tree (continuity/, liveness/) at derive time - the original
lines are never touched, exactly like live derivation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if REPO_SRC.exists():
    sys.path.insert(0, str(REPO_SRC))

from motherclank import continuity as cont          # noqa: E402
from motherclank import liveness as live            # noqa: E402


def _parse(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _in_window(ts: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse(ts)
    return dt is not None and start <= dt <= end


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read one JSONL file or every *.jsonl inside a dated directory."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    if path.is_dir():
        files = sorted(path.glob("*.jsonl"))
    else:
        files = [path]
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _line_hash(rec: dict[str, Any]) -> str:
    known = rec.get("content_hash") or rec.get("batch_hash") or rec.get("chain_hash")
    if known:
        return str(known)
    blob = json.dumps(rec, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _affected_clanks(rec: dict[str, Any]) -> list[str]:
    clanks = rec.get("clanks")
    if isinstance(clanks, dict):
        return sorted(clanks)
    if rec.get("clank_id"):
        return [str(rec["clank_id"])]
    return []


def _qualification(cid: str, ts: str, events, expectations) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if events:
        c = cont.continuity_context(events, cid, ts)
        ctx["continuity_state"] = c["continuity_state"]
        ctx["epoch_id"] = c.get("epoch_id")
        ctx["continuity_event_ids"] = c.get("active_event_ids", [])
    else:
        ctx["continuity_state"] = "UNKNOWN_CONTINUITY"
    exp = live.expectation_for(expectations, cid, ts) if expectations else None
    ctx["execution_policy"] = (exp or {}).get("policy", "UNKNOWN")
    return ctx


def reconcile(var_dir: Path, start: datetime, end: datetime,
              clank_filter: list[str] | None = None,
              as_of: str | None = None) -> dict[str, Any]:
    events, cont_warns = cont.load_events(var_dir)
    expectations, live_warns = live.load_expectations(var_dir)

    def wanted(clanks: list[str]) -> bool:
        return not clank_filter or any(c in clank_filter for c in clanks)

    findings: list[dict[str, Any]] = []

    for rec in _read_jsonl(var_dir / "snapshots" ):
        ts = rec.get("harvested_at_utc", "")
        if not _in_window(ts, start, end):
            continue
        for cid, block in (rec.get("clanks") or {}).items():
            if clank_filter and cid not in clank_filter:
                continue
            findings.append({
                "artifact": "snapshot",
                "timestamp": ts,
                "artifact_hash": _line_hash(rec),
                "clank_id": cid,
                "derived_claim": {
                    "operational_state": (block.get("status") or {}).get(
                        "operational_state", "UNKNOWN"),
                    "adapter_failed": block.get("observation") == "FAILED_ADAPTER",
                },
                "qualification": _qualification(cid, ts, events, expectations),
            })

    snapshots_by_hash = {r.get("content_hash"): r
                         for r in _read_jsonl(var_dir / "snapshots")}
    for rec in _read_jsonl(var_dir / "syntheses"):
        ts = rec.get("synthesized_at_utc", "")
        if not _in_window(ts, start, end):
            continue
        snap = snapshots_by_hash.get(rec.get("snapshot_hash"), {})
        snap_ts = snap.get("harvested_at_utc", "")
        for cid, claim in (rec.get("clanks") or {}).items():
            if clank_filter and cid not in clank_filter:
                continue
            findings.append({
                "artifact": "synthesis",
                "timestamp": ts,
                "artifact_hash": _line_hash(rec),
                "clank_id": cid,
                "derived_claim": {"state": claim.get("state"),
                                  "rules": claim.get("rules_applied"),
                                  "snapshot_observed_at": snap_ts},
                # THE key column: does incident evidence change this reading?
                "interpretation_changes_with_incident_evidence":
                    claim.get("state") != "UNKNOWN"
                    and _qualification(cid, snap_ts or ts, events,
                                       expectations).get("continuity_state",
                                                         "CONTINUOUS")
                    != "CONTINUOUS",
                "qualification": _qualification(cid, snap_ts or ts, events,
                                                expectations),
            })

    anomaly_records: list[tuple[str, dict[str, Any]]] = []
    for batch in _read_jsonl(var_dir / "anomalies"):
        bts = batch.get("batch_generated_from", "")
        if not _in_window(bts, start, end):
            continue
        for a in batch.get("anomalies", []):
            seen = a.get("last_seen") or a.get("first_seen") or bts
            if not (_in_window(a.get("first_seen"), start, end)
                    or _in_window(seen, start, end)):
                continue
            anomaly_records.append((bts, a))

    for a in anomaly_records:
        bts, a = a  # unpack tuple of (batch_ts, anomaly)
        cid = a.get("clank_id", "")
        if clank_filter and cid not in clank_filter:
            continue
        findings.append({
            "artifact": "anomaly",
            "timestamp": bts,
            "artifact_hash": _line_hash(a),
            "clank_id": cid,
            "derived_claim": {"type": a.get("type"),
                              "lifecycle": a.get("lifecycle"),
                              "subject": a.get("subject")},
            "already_continuity_qualified": bool(a.get("continuity_qualified")),
            "qualification": _qualification(cid, a.get("last_seen") or bts,
                                            events, expectations),
        })

    for klass, key in (("recommendations", "generated_from"),
                       ("qc_corpus", "generated_from")):
        for rec in _read_jsonl(var_dir / klass):
            ts = rec.get(key, "")
            if not _in_window(ts, start, end):
                continue
            findings.append({
                "artifact": klass,
                "timestamp": ts,
                "artifact_hash": _line_hash(rec),
                "clank_id": None,
                "derived_claim": {"record_count": rec.get("record_count"),
                                  "active_count": rec.get("active_count")},
            })

    for rec in _read_jsonl(var_dir / "soak"):
        latest = ""
        window = rec.get("window") or {}
        if isinstance(window, dict):
            latest = str(window.get("latest", ""))
        if not _in_window(latest, start, end):
            continue
        findings.append({"artifact": "soak_report", "timestamp": latest,
                         "artifact_hash": _line_hash(rec), "clank_id": None})

    if cont_warns or live_warns:
        findings.append({"artifact": "registry_warnings",
                         "timestamp": None, "artifact_hash": None,
                         "clank_id": None,
                         "warnings": cont_warns + live_warns})

    return {
        "reconciler_version": 1,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "clank_filter": clank_filter,
        "continuity_registry_hash": cont.registry_hash(events) if events else None,
        "finding_count": len(findings),
        "findings": findings,
        "note": ("READ-ONLY reconciliation; qualification is derive-time "
                 "context, no artifact was modified"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--var-dir", required=True, type=Path)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--clank", action="append", default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="write JSON report here (default: stdout)")
    args = ap.parse_args()

    start, end = _parse(args.start), _parse(args.end)
    if start is None or end is None:
        print("error: --start/--end must be ISO timestamps", file=sys.stderr)
        return 2
    report = reconcile(args.var_dir, start, end, args.clank)
    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"wrote {args.out} ({report['finding_count']} findings)")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
