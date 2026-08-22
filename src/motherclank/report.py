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
        if rollup.get("unsupported"):
            src = "unsupported"
        elif rollup.get("no_sources_recorded"):
            src = "UNKNOWN"
        else:
            src = "/".join(str(rollup[k]) for k in ("ok", "degraded", "failed", "blocked_zero", "unknown"))
        events = block.get("event_summary") or {}
        ev = _fmt(events.get("events_total")) if isinstance(events, dict) else _UNKNOWN
        qc = block.get("qc_summary") or {}
        qc_disp = qc.get("dispositions") if isinstance(qc, dict) else None
        qc_s = _fmt(qc_disp)
        notes_parts = []
        lr = block.get("last_run")
        if isinstance(lr, dict) and lr.get("status"):
            notes_parts.append(f"last_run={lr['status']}")
        epoch = block.get("current_epoch")
        if epoch is None and "current_epoch" in block:
            notes_parts.append("epoch=UNKNOWN")
        lines.append(f"| {cid} | {_state_of(block)} | {src} | {ev} | {qc_s} | {'; '.join(notes_parts) or '-'} |")
    lines.append("")
    lines.append("_All values are as observed by read-only adapters; UNKNOWN means the")
    lines.append("underlying system does not evidence it. Clank databases remain authoritative._")
    lines.append("")
    return "\n".join(lines)


def write_report(out_dir: Path, payload: dict[str, Any]) -> Path:
    rep_dir = out_dir / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    target = rep_dir / f"fleet-{payload['harvested_at_utc'].replace(':', '').replace('+0000', 'Z')}.md"
    target.write_text(render_report(payload))
    return target
