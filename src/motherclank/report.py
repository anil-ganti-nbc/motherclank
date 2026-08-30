"""Derived Markdown fleet report. Pure function of a snapshot payload."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import source_rollup

_UNKNOWN = "UNKNOWN"


def _fmt(value: Any) -> str:
    if value is None:
        return _UNKNOWN
    if isinstance(value, dict):
        inner = value.get("observation")
        if inner == "FAILED_ADAPTER":
            return f"ADAPTER_FAILED({value.get('error', '')[:60]})"
        return ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(value.items())[:4]) or "{}"
    return str(value)


def _state_of(clank_block: dict[str, Any]) -> str:
    status = clank_block.get("status")
    if isinstance(status, dict):
        raw = str(status.get("operational_state", _UNKNOWN)).split(".")[-1]
        return raw.upper() if raw and raw != _UNKNOWN.lower() else _UNKNOWN
    return _UNKNOWN


def render_report(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    ap = payload.get("harvested_at_utc", _UNKNOWN)
    lines.append("# Motherclank fleet report (DERIVED)")
    lines.append("")
    lines.append(f"- Harvested: {ap}")
    lines.append(f"- Inventory revision: {payload.get('inventory_revision', _UNKNOWN)}")
    lines.append(f"- Adapter contract: {payload.get('adapter_contract_versions', {}).get('adapter_contract_version', _UNKNOWN)}")
    lines.append(f"- Previous snapshot: {payload.get('previous_snapshot_hash') or 'none (first snapshot)'}")
    lines.append(f"- Snapshot hash: {payload.get('content_hash', _UNKNOWN)}")
    lines.append("")
    lines.append("| Clank | State | Sources ok/deg/fail/blocked/unknown | Events | QC dispositions | Notes |")
    lines.append("|---|---|---|---|---|---|")
    for cid in sorted(payload.get("clanks", {})):
        block = payload["clanks"][cid]
        if block.get("observation") == "FAILED_ADAPTER":
            lines.append(f"| {cid} | ADAPTER_FAILED | - | - | - | {block.get('error', '')[:80]} |")
            continue
        rollup = source_rollup(block.get("health"))
        status_extensions = (block.get("status") or {}).get("extensions", {})
        integrity_observer = status_extensions.get("health_semantics") == "integrity"
        if integrity_observer:
            src = "integrity"
        elif rollup.get("unsupported"):
            src = "unsupported"
        elif rollup.get("no_sources_recorded"):
            src = "UNKNOWN"
        else:
            src = "/".join(str(rollup[k]) for k in ("ok", "degraded", "failed", "blocked_zero", "unknown"))
        events = block.get("event_summary") or {}
        ev = "-" if integrity_observer else (_fmt(events.get("events_total")) if isinstance(events, dict) else _UNKNOWN)
        qc = block.get("qc_summary") or {}
        qc_disp = qc.get("dispositions") if isinstance(qc, dict) else None
        qc_s = "-" if integrity_observer else _fmt(qc_disp)
        notes_parts = []
        lr = block.get("last_run")
        if isinstance(lr, dict) and lr.get("status"):
            notes_parts.append(f"last_run={lr['status']}")
        epoch = block.get("current_epoch")
        if epoch is None and "current_epoch" in block:
            notes_parts.append("epoch=UNKNOWN")
        if integrity_observer:
            summary = (block.get("observer_snapshot") or {}).get("summary", {})
            if isinstance(summary, dict):
                notes_parts.append(
                    "integrity={}; rules={}; ratified_e4={}; open_triggers={}".format(
                        summary.get("integrity", _UNKNOWN),
                        summary.get("rules", _UNKNOWN),
                        summary.get("ratified_e4", _UNKNOWN),
                        summary.get("open_triggers", _UNKNOWN),
                    )
                )
        lines.append(f"| {cid} | {_state_of(block)} | {src} | {ev} | {qc_s} | {'; '.join(notes_parts) or '-'} |")
    lines.append("")
    lines.append("_All values are as observed by read-only adapters; UNKNOWN means the")
    lines.append("underlying system does not evidence it. Clank databases remain authoritative._")
    lines.append("")
    return chr(10).join(lines)


def write_report(out_dir: Path, payload: dict[str, Any]) -> Path:
    rep_dir = out_dir / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    target = rep_dir / f"fleet-{payload['harvested_at_utc'].replace(':', '').replace('+0000', 'Z')}.md"
    target.write_text(render_report(payload))
    return target


def render_synthesis(synth: dict[str, Any]) -> str:
    lines = [
        "# Motherclank fleet synthesis (DERIVED — M1)",
        "",
        f"- Synthesized: {synth.get('synthesized_at_utc', _UNKNOWN)}",
        f"- From snapshot: {synth.get('snapshot_hash', _UNKNOWN)} "
        f"(harvested {synth.get('snapshot_harvested_at', _UNKNOWN)})",
        f"- Fleet state: **{synth.get('fleet_state', _UNKNOWN)}** "
        f"(confidence: {synth.get('fleet_confidence', _UNKNOWN)})",
        f"- State counts: {_fmt(synth.get('state_counts'))}",
        f"- Previous synthesis: {synth.get('previous_synthesis_hash') or 'none (first)'}",
        f"- Synthesis hash: {synth.get('content_hash', _UNKNOWN)}",
        "",
        "| Clank | Derived state | Rules | Evidence (verbatim from snapshot) |",
        "|---|---|---|---|",
    ]
    for cid in sorted(synth.get("clanks", {})):
        c = synth["clanks"][cid]
        lines.append(
            f"| {cid} | {c['state']} | {', '.join(c['rules_applied']) or '-'} "
            f"| {'; '.join(c['evidence_fields']) or '-'} |"
        )
    lines += ["", "## Law 9 deployment-drift indicator", ""]
    drift = synth.get("law9_drift") or []
    if not drift:
        lines.append("_No checkout mapping provided for this run._")
    else:
        lines.append("| Clank | Checkout HEAD | Ledger SHA | Relationship |")
        lines.append("|---|---|---|---|")
        for row in drift:
            lines.append(
                f"| {row['clank']} | {row['checkout_head'][:12]} "
                f"| {str(row['ledger_sha'])[:12]} | {row['relationship']} |"
            )
    lines += [
        "",
        "_Downgrade-only synthesis: UNKNOWN evidence never upgrades to HEALTHY._",
        "_Clank databases remain authoritative; this document is derived._",
        "",
    ]
    return chr(10).join(lines)


def render_anomalies(batch: dict[str, Any]) -> str:
    lines = [
        "# Motherclank anomaly ledger (DERIVED — M2, deterministic)",
        "",
        f"- Generated from observations through: {batch.get('batch_generated_from', _UNKNOWN)}",
        f"- Detection rules: {batch.get('detection_rules_version', _UNKNOWN)}",
        f"- Active: {batch.get('active_count', 0)} · Recovered: {batch.get('recovered_count', 0)}",
        f"- Previous batch: {batch.get('previous_batch_hash') or 'none (first)'}",
        f"- Batch hash: {batch.get('batch_hash', _UNKNOWN)}",
        "",
        "| Lifecycle | Severity | Type | Clank | Subject | First seen | Last seen | Latest evidence |",
        "|---|---|---|---|---|---|---|---|",
    ]
    order = {"NEW": 0, "ONGOING": 1, "REOPENED": 0, "RECOVERED": 2}
    for a in sorted(batch.get("anomalies", []),
                    key=lambda x: (order.get(x["lifecycle"], 3), x["severity"],
                                   x["type"], x["clank_id"])):
        latest = (a["evidence"] or [{}])[-1].get("detail", "")
        lines.append(
            f"| {a['lifecycle']} | {a['severity']} | {a['type']} "
            f"| {a['clank_id']} | {a['subject'][:28]} "
            f"| {a['first_seen'][:19]} | {a['last_seen'][:19]} "
            f"| {latest[:70]} |"
        )
    lines += [
        "",
        "_Deterministic rules only. UNKNOWN never proves failure; transitions are",
        "judged between two KNOWN observations. Recovered anomalies are retained._",
        "",
    ]
    return chr(10).join(lines)


def render_recommendations(batch: dict[str, Any]) -> str:
    lines = [
        "# Motherclank operator recommendations (ADVISORY — M3, deterministic)",
        "",
        f"- Generated from observations through: {batch.get('generated_from', _UNKNOWN)}",
        f"- Rules: {batch.get('rules_version', _UNKNOWN)} · "
        f"anomaly batch: {str(batch.get('anomaly_batch_hash'))[:20]}",
        f"- Active: {batch.get('active_count', 0)} · Closed: {batch.get('closed_count', 0)}",
        f"- Previous batch: {batch.get('previous_batch_hash') or 'none (first)'}",
        f"- Batch hash: {batch.get('batch_hash', _UNKNOWN)}",
        "",
        "_Recommendations are advisory text derived from the anomaly ledger.",
        "The operator owns every decision; Motherclank executes nothing._",
        "",
        "| Status | Priority | Category | Clank | Recommendation | Cited anomalies (latest evidence) |",
        "|---|---|---|---|---|---|",
    ]
    order = {"P1": 0, "P2": 1, "P3": 2}
    for r0 in sorted(batch.get("recommendations", []),
                     key=lambda x: (x["status"], order.get(x["priority"], 3),
                                    x["clank_id"])):
        cites = "; ".join(
            f"{c['type']}[{c['lifecycle']}] {c['latest_evidence'][:48]}"
            for c in (r0["cited_anomalies"] + r0["resolved_citations"])[:3]
        )
        lines.append(
            f"| {r0['status']} | {r0['priority']} | {r0['category']} "
            f"| {r0['clank_id']} | {r0['title'][:70]} | {cites[:110]} |"
        )
        lines.append(f"|  |  |  |  | -> *{r0['recommended_action'][:120]}* |  |")
    lines.append("")
    return chr(10).join(lines)
