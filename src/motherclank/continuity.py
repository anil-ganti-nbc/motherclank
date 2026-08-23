"""Motherclank F6 — observational continuity / incident semantics.

General fleet capability for representing KNOWN destructive discontinuities
(DB loss, restore-from-backup, fresh baseline, scheduler outage) so that
absence, reset counters, restored older state, or apparent source
disappearance/reappearance can never masquerade as organic fleet behaviour.

Design contract (ADR-0006 draft; ADR-0002/0005 boundaries preserved):

- Events live in an APPEND-ONLY registry file owned by operators/systems:
  ``<var-dir>/continuity/continuity-events.jsonl``. Old lines are never
  edited; later knowledge appends new evidence. Historical Motherclank
  artifacts remain untouched records of what was known at the time;
  qualification happens at DERIVE time, not by rewriting history.
- Diagnostic Clank's adapter plane remains the authoritative EVIDENCE source
  (epoch markers, backup posture); this registry carries the incident record
  and cites that evidence. Motherclank only consumes and derives.
- UNKNOWN means UNKNOWN: an event with ``effective_end: null`` is an open,
  unbounded discontinuity. Absence is never converted to zero. Restoration
  never implies uninterrupted continuity. A fresh baseline is never novelty.
  Every derived claim can name the epoch/continuity context it came from.

This module is pure derivation over recorded data: no clocks, no network,
no writes outside Motherclank's own outputs, no mutation authority.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTINUITY_SCHEMA_VERSION = 1

EVENT_TYPES = (
    "DATA_LOSS",
    "RESTORE_FROM_BACKUP",
    "NEW_BASELINE",
    "EPOCH_BOUNDARY",
    "OBSERVATION_GAP",
    "SCHEDULER_OUTAGE",
    "UNKNOWN_CONTINUITY",
)

# Orthogonal continuity dimension for M1 synthesis. Deliberately independent
# of HEALTHY/DEGRADED/FAILED/UNKNOWN operational states: a Clank may be
# operationally HEALTHY while continuity is GAP_KNOWN.
CONTINUITY_STATES = (
    "CONTINUOUS",        # no qualifying events apply at this instant
    "GAP_KNOWN",         # inside (or bounded by) a known observation gap
    "RESTORED_HISTORY",  # serving restored backup state; pre-loss link severed
    "NEW_EPOCH",         # hard epoch discontinuity; histories must not merge
    "UNKNOWN_CONTINUITY",
)

REQUIRED_FIELDS = (
    "event_id",
    "clank_id",
    "instance_id",
    "lane_id",
    "event_type",
    "effective_start",
    "discovered_at",
    "origin",
)

_EPOCH_TYPES = {"EPOCH_BOUNDARY", "NEW_BASELINE", "DATA_LOSS"}
_GAP_TYPES = {"OBSERVATION_GAP", "SCHEDULER_OUTAGE", "DATA_LOSS", "UNKNOWN_CONTINUITY"}


def _parse(value: Any) -> Any:
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def content_hash(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "content_hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def validate_event(record: dict[str, Any]) -> list[str]:
    """Return a list of contract violations; empty means valid."""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required field: {field}")
    if record.get("event_type") not in EVENT_TYPES:
        errors.append(f"invalid event_type: {record.get('event_type')!r}")
    if record.get("origin") not in ("operator", "system"):
        errors.append(f"origin must be 'operator' or 'system': {record.get('origin')!r}")
    if _parse(record.get("effective_start")) is None:
        errors.append("effective_start is not an ISO timestamp")
    end = record.get("effective_end")
    if end is not None:
        if _parse(end) is None:
            errors.append("effective_end is not null or an ISO timestamp")
        elif _parse(end) < _parse(record.get("effective_start")):
            errors.append("effective_end precedes effective_start")
    if _parse(record.get("discovered_at")) is None:
        errors.append("discovered_at is not an ISO timestamp")
    expected = record.get("content_hash")
    if expected is not None and expected != content_hash(record):
        errors.append("content_hash mismatch")
    return errors


def make_event(**fields: Any) -> dict[str, Any]:
    """Build a validated event, filling neutral defaults; raises on violation."""
    record = {
        "schema_version": CONTINUITY_SCHEMA_VERSION,
        "instance_id": fields.pop("instance_id", "UNKNOWN"),
        "lane_id": fields.pop("lane_id", "UNKNOWN"),
        "effective_end": fields.pop("effective_end", None),
        "previous_epoch_id": fields.pop("previous_epoch_id", None),
        "new_epoch_id": fields.pop("new_epoch_id", None),
        "evidence_refs": fields.pop("evidence_refs", []),
        "notes": fields.pop("notes", ""),
        **fields,
    }
    errors = validate_event(record)
    if errors:
        raise ValueError("invalid continuity event: " + "; ".join(errors))
    record["content_hash"] = content_hash(record)
    return record


def load_events(var_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load the append-only continuity registry. Tolerant: malformed lines are
    reported as warnings and skipped, never silently repaired."""
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    path = Path(var_dir) / "continuity" / "continuity-events.jsonl"
    if not path.exists():
        return events, warnings
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"continuity:{lineno}: unparsable line skipped ({exc})")
            continue
        errors = validate_event(rec)
        if errors:
            warnings.append(f"continuity:{lineno}: invalid event skipped ({'; '.join(errors)})")
            continue
        events.append(rec)
    events.sort(key=lambda e: (str(e.get("effective_start")), str(e.get("event_id"))))
    return events, warnings


def registry_hash(events: list[dict[str, Any]]) -> str:
    """Chain hash of the loaded registry: identifies WHICH continuity context
    a given artifact was derived against."""
    blob = json.dumps(
        [e.get("content_hash") for e in events], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def _active_at(event: dict[str, Any], at: str) -> bool:
    t = _parse(at)
    start = _parse(event.get("effective_start"))
    end = _parse(event.get("effective_end"))
    if t is None or start is None:
        return False
    if t < start:
        return False
    if end is not None and t >= end:
        return False
    return True


def active_events(events: list[dict[str, Any]], clank_id: str,
                  at: str) -> list[dict[str, Any]]:
    """Events in force for one Clank at one instant."""
    out = [e for e in events if e.get("clank_id") == clank_id and _active_at(e, at)]
    out.sort(key=lambda e: str(e.get("effective_start")))
    return out


def _epoch_at(events: list[dict[str, Any]], clank_id: str, at: str) -> str:
    """Latest declared epoch identifier effective at ``at``; UNKNOWN when the
    registry does not establish one (which is itself honest)."""
    epoch = None
    for e in active_events(events, clank_id, at):
        marker = e.get("new_epoch_id")
        if e["event_type"] in _EPOCH_TYPES and marker:
            epoch = marker
    return epoch or "UNKNOWN"


def continuity_context(events: list[dict[str, Any]], clank_id: str,
                       at: str) -> dict[str, Any]:
    """The derive-time continuity qualification for one Clank at one instant."""
    applicable = active_events(events, clank_id, at)
    if not applicable:
        return {
            "continuity_state": "CONTINUOUS",
            "epoch_id": _epoch_at(events, clank_id, at),
            "active_event_ids": [],
            "evidence_refs": [],
        }
    types = {e["event_type"] for e in applicable}
    if "NEW_BASELINE" in types or "EPOCH_BOUNDARY" in types:
        state = "NEW_EPOCH"
    elif "RESTORE_FROM_BACKUP" in types:
        state = "RESTORED_HISTORY"
    elif types & _GAP_TYPES:
        state = "GAP_KNOWN"
    else:
        state = "UNKNOWN_CONTINUITY"
    return {
        "continuity_state": state,
        "epoch_id": _epoch_at(events, clank_id, at),
        "active_event_ids": [e["event_id"] for e in applicable],
        "evidence_refs": sorted({ref for e in applicable
                                 for ref in (e.get("evidence_refs") or [])}),
        "explanation": "; ".join(
            f"{e['event_type']}({e['event_id']}) from "
            f"{e['effective_start']} to {e.get('effective_end') or 'open'}"
            for e in applicable),
    }


def annotate_snapshot(snapshot_payload: dict[str, Any],
                      events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive-time annotation of a snapshot payload with continuity context.
    Returns a copy; the original append-only artifact is never modified."""
    at = snapshot_payload.get("harvested_at_utc", "")
    out = dict(snapshot_payload)
    out["continuity_registry_hash"] = registry_hash(events) if events else None
    clanks = {}
    for cid, block in out.get("clanks", {}).items():
        ctx = continuity_context(events, cid, at)
        annotated = dict(block)
        annotated["continuity"] = ctx
        clanks[cid] = annotated
    out["clanks"] = clanks
    return out


def incident_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explicit M2-visible records for every registered event, so incidents
    appear in the anomaly ledger as themselves instead of as fake organic
    transitions. Deterministic; derived purely from the registry."""
    records: list[dict[str, Any]] = []
    for e in events:
        aid = "cont-" + hashlib.sha256(e["event_id"].encode()).hexdigest()[:16]
        raw = {
            "anomaly_id": aid,
            "type": "CONTINUITY_EVENT",
            "severity": "INFO",
            "clank_id": e["clank_id"],
            "subject": e["event_type"],
            "first_seen": e["effective_start"],
            "last_seen": e.get("effective_end") or e.get("discovered_at", ""),
            "lifecycle": "OPEN" if e.get("effective_end") is None else "CLOSED",
            "evidence": [{
                "observed_at": e.get("discovered_at", ""),
                "detail": (f"{e['event_type']} on {e['clank_id']}"
                           f"[{e.get('lane_id', 'UNKNOWN')}] effective "
                           f"{e['effective_start']}..{e.get('effective_end') or 'open'}"
                           + (f"; notes: {e['notes']}" if e.get("notes") else "")),
            }],
            "continuity_event_ids": [e["event_id"]],
            "provenance": {
                "derived_by": "motherclank-m2/continuity",
                "deterministic": True,
                "registry_hash": registry_hash([e]),
                "origin": e.get("origin"),
            },
        }
        raw["chain_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        records.append(raw)
    return records


def qualify_anomalies(anomalies: list[dict[str, Any]],
                      snapshots: list[dict[str, Any]],
                      events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate anomalies derived from observations taken inside a known
    discontinuity. Qualification is additive: evidence is never deleted,
    upgraded, or downgraded — only explained."""
    if not events:
        return anomalies
    ctx_by_snap: dict[str, dict[str, dict[str, Any]]] = {}
    for snap in snapshots:
        at = snap.get("harvested_at_utc", "")
        ctx_by_snap[at] = {}
        for cid in snap.get("clanks", {}):
            ctx_by_snap[at][cid] = continuity_context(events, cid, at)

    def ctx_for(clank_id: str, seen_at: str) -> dict[str, Any]:
        best_key, best = "", None
        for key in ctx_by_snap:
            if key >= seen_at and (best_key == "" or key < best_key):
                best_key, best = key, ctx_by_snap[key].get(clank_id)
        if best is None:
            for key in sorted(ctx_by_snap, reverse=True):
                cand = ctx_by_snap[key].get(clank_id)
                if cand:
                    return cand
        return best or {"continuity_state": "UNKNOWN_CONTINUITY",
                        "active_event_ids": [], "evidence_refs": []}

    out = []
    for a in anomalies:
        if a.get("type") == "CONTINUITY_EVENT":
            out.append(a)
            continue
        seen_at = a.get("last_seen", "") or a.get("first_seen", "")
        ctx = ctx_for(a.get("clank_id", ""), seen_at)
        if ctx["continuity_state"] != "CONTINUOUS":
            qualified = dict(a)
            qualified["continuity_qualified"] = True
            qualified["continuity_state"] = ctx["continuity_state"]
            qualified["continuity_event_ids"] = ctx["active_event_ids"]
            out.append(qualified)
        else:
            out.append(a)
    return out
