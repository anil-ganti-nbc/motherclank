"""Motherclank F6c — storage survivability evidence model (ADR-0007 draft).

Motherclank OBSERVES protection posture; it never creates, deletes, or
restores anything. Evidence arrives as append-only, content-hashed records
(``<var>/survivability/survivability-events.jsonl``) authored by operators,
Clank-side backup tooling, or the Diagnostic Clank adapter plane.

Vocabulary deliberately aligns with diagnostic-clank's manifest-style
backup records (PR #2, agent/backup-restore-v01) while remaining
storage-neutral: SQLite files, Docker volumes, direct paths, and future
stores are all expressible.

Verification discipline (each level REQUIRES the previous):

    BACKUP_CREATED            -> a recovery point exists          (UNVERIFIED)
    + INTEGRITY_VERIFIED      -> checked, not proven restorable   (INTEGRITY_VERIFIED)
    + RESTORE_DRILL_PASSED    -> isolated restore succeeded       (RESTORE_VERIFIED)

An off-host copy satisfies durable redundancy ONLY when its transfer record
declares destination_class="durable". Temporary scratch copies (the current
ACT-011 state for the incident lanes) are recorded honestly and do NOT
close the off-host gate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .continuity import _parse

SURVIVABILITY_SCHEMA_VERSION = 1

RECORD_TYPES = (
    "BACKUP_CREATED",
    "BACKUP_INTEGRITY_VERIFIED",
    "BACKUP_TRANSFERRED_OFFHOST",
    "RESTORE_DRILL_STARTED",
    "RESTORE_DRILL_PASSED",
    "RESTORE_DRILL_FAILED",
    "RECOVERY_POINT_EXPIRED",
    "PRIMARY_STATE_RECREATED",
    "CONTINUITY_GAP_DECLARED",
)

DESTINATION_CLASSES = ("durable", "temporary_scratch", "unknown")

PROTECTION_STATES = (
    "NONE",
    "UNVERIFIED",
    "INTEGRITY_VERIFIED",
    "RESTORE_VERIFIED",
)

REQUIRED_FIELDS = (
    "record_id",
    "record_type",
    "clank_id",
    "created_at",
    "origin",
)


def content_hash(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "content_hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def validate_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required field: {field}")
    if record.get("record_type") not in RECORD_TYPES:
        errors.append(f"invalid record_type: {record.get('record_type')!r}")
    if record.get("origin") not in ("operator", "system", "tooling"):
        errors.append(f"origin must be operator|system|tooling: {record.get('origin')!r}")
    if _parse(record.get("created_at")) is None:
        errors.append("created_at is not an ISO timestamp")
    dest = record.get("destination_class")
    if dest is not None and dest not in DESTINATION_CLASSES:
        errors.append(f"invalid destination_class: {dest!r}")
    expected = record.get("content_hash")
    if expected is not None and expected != content_hash(record):
        errors.append("content_hash mismatch")
    return errors


def make_record(**fields: Any) -> dict[str, Any]:
    record = {
        "schema_version": SURVIVABILITY_SCHEMA_VERSION,
        "instance_id": fields.pop("instance_id", "UNKNOWN"),
        "lane_id": fields.pop("lane_id", "UNKNOWN"),
        "epoch_id": fields.pop("epoch_id", None),
        "artifact_id": fields.pop("artifact_id", None),
        "hash": fields.pop("hash", None),
        "verification_method": fields.pop("verification_method", None),
        "operator_or_tool": fields.pop("operator_or_tool", "UNKNOWN"),
        "relates_to": fields.pop("relates_to", None),
        "destination_class": fields.pop("destination_class", None),
        "continuity_event_ids": fields.pop("continuity_event_ids", []),
        "notes": fields.pop("notes", ""),
        **fields,
    }
    errors = validate_record(record)
    if errors:
        raise ValueError("invalid survivability record: " + "; ".join(errors))
    record["content_hash"] = content_hash(record)
    return record


def load_records(var_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    path = Path(var_dir) / "survivability" / "survivability-events.jsonl"
    if not path.exists():
        return records, warnings
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"survivability:{lineno}: unparsable line skipped ({exc})")
            continue
        errors = validate_record(rec)
        if errors:
            warnings.append(f"survivability:{lineno}: invalid record skipped "
                            f"({'; '.join(errors)})")
            continue
        # P-4 hardening: a recovery point without an artifact hash is still
        # evidence, but it is NOT cryptographically identified. Surface the
        # distinction at ingest; never launder it later.
        if rec.get("record_type") == "BACKUP_CREATED" and not rec.get("hash"):
            artifact = rec.get("artifact_id") or "unidentified artifact"
            warnings.append(
                "RECOVERY_POINT_WITHOUT_ARTIFACT_HASH: "
                f"{rec.get('record_id')} ({artifact}) - backup known but not "
                "cryptographically identified")
        records.append(rec)
    return records, warnings


def _for_lane(records: list[dict[str, Any]], clank_id: str,
              lane_id: str | None) -> list[dict[str, Any]]:
    sel = [r for r in records if r.get("clank_id") == clank_id]
    if lane_id is not None and lane_id != "UNKNOWN":
        sel = [r for r in sel if r.get("lane_id") in (lane_id, "UNKNOWN")]
    sel.sort(key=lambda r: str(r.get("created_at")))
    return sel


def derive_protection(records: list[dict[str, Any]], clank_id: str,
                      lane_id: str | None = None, *,
                      as_of: str | None = None) -> dict[str, Any]:
    """Derive per-lane protection status from evidence only.

    Rules enforced here are golden-incident contract:
      - file-existence claims never verify themselves (G6);
      - VERIFIED requires a passed restore drill linked to that recovery
        point (G7);
      - off-host durability requires an explicitly durable destination (G8).
    """
    lane_records = _for_lane(records, clank_id, lane_id)
    created = [r for r in lane_records if r["record_type"] == "BACKUP_CREATED"]
    if not created:
        return {
            "protection_state": "NONE",
            "newest_recovery_point": None,
            "off_host_durable": False,
            "open_gaps": ["no recovery point evidence"],
            "evidence_record_count": len(lane_records),
        }

    newest = created[-1]
    artifact = newest.get("artifact_id")
    integrity = any(r["record_type"] == "BACKUP_INTEGRITY_VERIFIED"
                    and r.get("relates_to") == artifact for r in lane_records)
    drill_passed = any(r["record_type"] == "RESTORE_DRILL_PASSED"
                       and r.get("relates_to") == artifact for r in lane_records)
    drill_failed = any(r["record_type"] == "RESTORE_DRILL_FAILED"
                       and r.get("relates_to") == artifact for r in lane_records)
    offhost_durable = any(r["record_type"] == "BACKUP_TRANSFERRED_OFFHOST"
                          and r.get("relates_to") == artifact
                          and r.get("destination_class") == "durable"
                          for r in lane_records)
    expired = any(r["record_type"] == "RECOVERY_POINT_EXPIRED"
                  and r.get("relates_to") == artifact for r in lane_records)

    if expired:
        state = "NONE"
    elif drill_failed:
        state = "INTEGRITY_VERIFIED" if integrity else "UNVERIFIED"
    elif drill_passed:
        state = "RESTORE_VERIFIED"
    elif integrity:
        state = "INTEGRITY_VERIFIED"
    else:
        state = "UNVERIFIED"

    open_gaps: list[str] = []
    if state in ("NONE", "UNVERIFIED"):
        open_gaps.append("recovery point not integrity-verified")
    if state != "RESTORE_VERIFIED":
        open_gaps.append("no passed restore drill for newest recovery point")
    if not offhost_durable:
        open_gaps.append("no durable off-host copy")

    rpo_seconds = None
    ref = as_of or newest.get("created_at")
    t_ref, t_created = _parse(ref), _parse(newest.get("created_at"))
    if t_ref is not None and t_created is not None:
        rpo_seconds = max(0.0, (t_ref - t_created).total_seconds())

    return {
        "protection_state": state,
        "newest_recovery_point": {
            "artifact_id": artifact,
            "created_at": newest.get("created_at"),
            "integrity_verified": integrity,
            "restore_verified": drill_passed,
            "restore_drill_failed": drill_failed,
            "expired": expired,
            "off_host_durable": offhost_durable,
            "hash": newest.get("hash"),
            # separate claim: a hash present means the RP is cryptographically
            # identified; its absence never downgrades the verification chain
            "cryptographically_identified": bool(newest.get("hash")),
        },
        "rpo_estimate_seconds": rpo_seconds,
        "off_host_durable": offhost_durable,
        "open_gaps": open_gaps,
        "evidence_record_count": len(lane_records),
        "orthogonal_to_operational_health": True,
    }
