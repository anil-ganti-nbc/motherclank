"""Motherclank QC Soak — periodic report generator (M4.5 addendum).

Reads the append-only qc_corpus history and produces a concise soak report:
per-Clank coverage + deltas vs previous batch, new labels, corrections,
unmapped vocabulary, QC-surface adapter failures, provenance-integrity check,
and M5 entry-gate scoring (Axis B ONLY — never Clank maturity/promotion).

Deterministic: all timestamps come from recorded batch generated_from values;
an optional --as-of anchors 'now' for soak-day math (default: latest batch).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .qc_corpus import read_previous_qc_batch  # noqa: F401  (re-export convenience)
from . import SNAPSHOT_SCHEMA_VERSION

SOAK_REPORT_VERSION = "soak-r1"

# M5_ENTRY_CRITERIA.md thresholds (Axis B)
GATES = {
    "G1_corpus_size": {"per_lane_records": 50, "lanes_required": 2},
    "G2_diversity": {"distinct_dispositions": 3, "min_per_disposition": 5},
    "G3_correction_rate": {"min_rate": 0.05},
    "G4_review_rate": {"min_rate": 0.20, "lanes_required": 1},
    "G5_unmapped_share": {"max_rate": 0.10},
    "G6_soak_days": {"min_days": 28},
    "G7_provenance_integrity": {},
}


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


from datetime import timezone  # noqa: E402


def load_batches(var_dir: Path) -> list[dict[str, Any]]:
    d = var_dir / "qc_corpus"
    batches = []
    if d.exists():
        for file in sorted(d.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    batches.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    batches.sort(key=lambda b: b.get("generated_from", ""))
    return batches


def provenance_integrity(batches: list[dict[str, Any]]) -> dict[str, Any]:
    """M5 gate G7: every record carries contract version + ingestion snapshot
    hash; every supersedes target exists as some earlier record's hash."""
    total = missing = 0
    known_hashes: set[str] = set()
    broken_links = 0
    for batch in batches:
        for rec in (batch.get("corpus") or {}).get("records", []):
            total += 1
            if not rec.get("ingestion_snapshot_hash") or rec.get("ingestion_snapshot_hash") == "no-snapshot":
                missing += 1
            sup = rec.get("supersedes")
            if sup and sup not in known_hashes:
                broken_links += 1
            known_hashes.add(rec.get("content_hash"))
    ok = total > 0 and missing == 0 and broken_links == 0
    return {"records_checked": total, "missing_snapshot_hash": missing,
            "broken_supersedes_links": broken_links, "pass": ok}


def score_gates(coverage: dict[str, Any], integrity: dict[str, Any],
                first_ts: str | None, latest_ts: str | None) -> dict[str, dict[str, Any]]:
    lanes_active = {cid: c for cid, c in coverage.items()
                    if isinstance(c.get("total_records"), int)}
    results: dict[str, dict[str, Any]] = {}

    # G1 corpus size
    qualifying = [cid for cid, c in lanes_active.items()
                  if c["total_records"] >= GATES["G1_corpus_size"]["per_lane_records"]]
    results["G1_corpus_size"] = {
        "state": "PASS" if len(qualifying) >= GATES["G1_corpus_size"]["lanes_required"]
                 else "NOT-YET-MATURE",
        "detail": {"qualifying_lanes": qualifying}}

    # G2 diversity
    div_ok = []
    for cid, c in lanes_active.items():
        dist = c.get("disposition_distribution") or {}
        strong = sum(1 for v in dist.values() if v >= GATES["G2_diversity"]["min_per_disposition"])
        if len(dist) >= GATES["G2_diversity"]["distinct_dispositions"] and strong >= 1:
            div_ok.append(cid)
    results["G2_diversity"] = {
        "state": "PASS" if len(div_ok) >= 1 else "NOT-YET-MATURE",
        "detail": {"qualifying_lanes": div_ok}}

    # G3 correction rate
    rates = [(cid, c.get("correction_rate")) for cid, c in coverage.items()]
    g3 = any(isinstance(r, (int, float)) and r >= GATES["G3_correction_rate"]["min_rate"]
             for _, r in rates)
    results["G3_correction_rate"] = {"state": "PASS" if g3 else "NOT-YET-MATURE",
                                     "detail": dict(rates)}

    # G4 review rate
    g4 = any(isinstance(c.get("review_rate"), (int, float))
             and c["review_rate"] >= GATES["G4_review_rate"]["min_rate"]
             for c in coverage.values())
    results["G4_review_rate"] = {"state": "PASS" if g4 else "NOT-YET-MATURE",
                                 "detail": {k: c.get("review_rate")
                                            for k, c in coverage.items()}}

    # G5 unmapped share (only meaningful where records exist)
    g5_candidates = [c.get("unmapped_rate") for c in coverage.values()
                     if isinstance(c.get("unmapped_rate"), (int, float))]
    g5 = bool(g5_candidates) and all(r <= GATES["G5_unmapped_share"]["max_rate"]
                                     for r in g5_candidates)
    results["G5_unmapped_share"] = {"state": "PASS" if g5 else ("FAIL" if g5_candidates else "NOT-YET-MATURE"),
                                    "detail": {k: c.get("unmapped_rate")
                                               for k, c in coverage.items()}}

    # G6 soak days
    f, l = _parse(first_ts or ""), _parse(latest_ts or "")
    days = ((l - f).total_seconds() / 86400.0) if f and l else None
    results["G6_soak_days"] = {
        "state": ("PASS" if isinstance(days, float) and days >= GATES["G6_soak_days"]["min_days"]
                  else "NOT-YET-MATURE"),
        "detail": {"days_elapsed": round(days, 2) if isinstance(days, float) else None,
                   "first_batch": first_ts, "latest_batch": latest_ts}}

    # G7 provenance
    results["G7_provenance_integrity"] = {
        "state": "PASS" if integrity.get("pass") else
                 ("NOT-YET-MATURE" if integrity.get("records_checked", 0) == 0 else "FAIL"),
        "detail": integrity}
    return results


def build_soak_report(var_dir: Path, *, as_of: str | None = None,
                      batches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    batches = batches if batches is not None else load_batches(var_dir)
    latest = batches[-1] if batches else None
    previous = batches[-2] if len(batches) >= 2 else None
    warnings: list[str] = []
    if not latest:
        warnings.append("no qc_corpus batches found")

    coverage = (latest or {}).get("coverage") or {}
    per_clank = {}
    prev_cov = (previous or {}).get("coverage") or {}
    surface_failures = []
    new_labels_total = 0
    corrections_since_prev = []

    prev_record_keys = set()
    if previous:
        for r in (previous.get("corpus") or {}).get("records", []):
            prev_record_keys.add((r["corpus_id"], r["content_hash"]))
            if r.get("fleet_disposition"):
                pass

    current_records = (latest or {}).get("corpus", {}).get("records", [])
    seen_now = set()
    for r in current_records:
        key = (r["corpus_id"], r["content_hash"])
        seen_now.add(key)
        if key not in prev_record_keys:
            fd = r.get("fleet_disposition")
            if fd and fd != "UNMAPPED":
                new_labels_total += 1
        if r.get("supersedes"):
            corrections_since_prev.append({
                "corpus_id": r["corpus_id"], "clank_id": r["clank_id"],
                "new_raw": r.get("raw_disposition"),
                "superseded_raw": r.get("superseded_raw_disposition")})

    for cid, cov in sorted(coverage.items()):
        block = (latest.get("corpus") or {}).get("clanks", {}).get(cid, {})
        if block.get("observation") == "FAILED_ADAPTER":
            surface_failures.append({"clank_id": cid,
                                     "error": block.get("error", "")[:120]})
        pcov = prev_cov.get(cid) or {}
        delta_new = None
        if isinstance(pcov.get("total_records"), int):
            delta_new = cov.get("total_records", 0) - pcov["total_records"]
        per_clank[cid] = {
            "eligible_items": cov.get("eligible_items"),
            "reviewed_items": cov.get("total_records"),
            "review_rate": cov.get("review_rate"),
            "disposition_distribution": cov.get("disposition_distribution"),
            "correction_rate": cov.get("correction_rate"),
            "unmapped_rate": cov.get("unmapped_rate"),
            "new_records_since_previous": delta_new,
        }

    # unmapped vocabulary accumulated across history
    unmapped_vocab: dict[str, set] = {}
    for b in batches:
        for r in (b.get("corpus") or {}).get("records", []):
            if r.get("fleet_disposition") == "UNMAPPED" and r.get("raw_disposition"):
                unmapped_vocab.setdefault(r["clank_id"], set()).add(
                    str(r["raw_disposition"]))
    unmapped_out = {k: sorted(v) for k, v in sorted(unmapped_vocab.items())}

    integrity = provenance_integrity(batches)
    first_ts = batches[0].get("generated_from") if batches else None
    latest_ts = as_of or (latest or {}).get("generated_from")
    gates = score_gates(coverage, integrity, first_ts, latest_ts)

    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": ("DERIVED QC-soak report — Axis B (Motherclank M5 "
                          "readiness) only; says nothing about Clank maturity "
                          "or promotion"),
        "report_version": SOAK_REPORT_VERSION,
        "batches_observed": len(batches),
        "window": {"first": first_ts, "latest": latest_ts, "as_of": as_of},
        "per_clank": per_clank,
        "new_labels_since_previous": new_labels_total,
        "corrections_since_previous": corrections_since_prev,
        "unmapped_vocabulary": unmapped_out,
        "qc_surface_failures": surface_failures,
        "provenance_integrity": integrity,
        "m5_gates_axis_b_only": gates,
        "boundary_note": ("Gate outcomes NEVER promote a Clank, terminate its "
                          "development soak, or change deployment status."),
    }
    payload["report_hash"] = "sha256:" + hashlib.sha256(
        json.dumps({k: v for k, v in payload.items() if k != "report_hash"},
                   sort_keys=True, default=str).encode()).hexdigest()
    return payload, warnings


def append_report(out_dir: Path, payload: dict[str, Any]) -> Path:
    day = str(payload["window"]["latest"])[:10] or "unknown"
    d = out_dir / "soak"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return d / f"{day}.jsonl"


def render_soak(payload: dict[str, Any]) -> str:
    lines = [
        "# Motherclank QC Soak Report (DERIVED — Axis B only)",
        "",
        f"- Window: {payload['window']['first']} → {payload['window']['latest']}",
        f"- Batches observed: {payload['batches_observed']}",
        f"- Boundary: {payload['boundary_note']}",
        "",
        "| Clank | Eligible | Reviewed | Review rate | Corrections | Unmapped | New since prev |",
        "|---|---|---|---|---|---|---|",
    ]
    for cid, c in payload["per_clank"].items():
        rr = c.get("review_rate")
        lines.append(
            f"| {cid} | {c.get('eligible_items', 'UNKNOWN')} "
            f"| {c.get('reviewed_items', 0)} "
            f"| {(f'{rr:.1%}' if isinstance(rr, (int, float)) else 'UNKNOWN')} "
            f"| {c.get('correction_rate', 'UNKNOWN')} "
            f"| {c.get('unmapped_rate', 'UNKNOWN')} "
            f"| {c.get('new_records_since_previous', 'UNKNOWN')} |")
    lines += ["", "## M5 entry gates (Axis B)", "",
              "| Gate | State | Detail |", "|---|---|---|"]
    for gate, res in payload["m5_gates_axis_b_only"].items():
        detail = json.dumps(res.get("detail"), default=str)[:80]
        lines.append(f"| {gate} | **{res['state']}** | {detail} |")
    if payload["qc_surface_failures"]:
        lines += ["", "## QC surface failures", ""]
        for f in payload["qc_surface_failures"]:
            lines.append(f"- {f['clank_id']}: {f['error']}")
    if payload["corrections_since_previous"]:
        lines += ["", "## Corrections since previous report", ""]
        for c in payload["corrections_since_previous"]:
            lines.append(f"- {c['clank_id']}: {c['superseded_raw']} → "
                         f"{c['new_raw']} ({c['corpus_id']})")
    uv_lines = [f"- {k}: {v}" for k, v in payload["unmapped_vocabulary"].items()]
    if uv_lines:
        lines += ["", "## Unmapped vocabulary (accumulated)"] + uv_lines
    lines.append("")
    return chr(10).join(lines)
