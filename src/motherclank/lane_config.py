"""P-4.3 — canonical Lane Config contract (declaration, never observation).

One validated model replacing expectation/config sprawl. A lane config is a
DECLARATION about how a lane is expected to execute; it can never manufacture
an observation and observations can never silently rewrite it.

Migrated losslessly from the existing expectations registry (same fields,
same UNKNOWN discipline), plus explicit scheduler-type and identity rules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .continuity import _parse
from .liveness import (
    DORMANT_POLICIES,
    MATERIALIZATION_POLICIES,
    EXECUTION_POLICIES,
)

LANE_CONFIG_SPEC_VERSION = "1"

SCHEDULER_TYPES = ("cron", "systemd_system", "systemd_user", "manual",
                   "retired", "other")

REQUIRED_FIELDS = ("clank_id", "instance_id", "lane_id",
                   "execution_policy", "authority")


def field_name(f: str) -> str:
    return f


def content_hash(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "content_hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def validate_config(record: dict[str, Any]) -> list[str]:
    """Contract violations; empty = valid. Contradictions are violations,
    not warnings: a config that contradicts itself cannot honestly declare
    anything."""
    errors: list[str] = []
    for f in REQUIRED_FIELDS:
        v = record.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            if not (f == "authority" and record.get("policy") in
                    DORMANT_POLICIES):
                errors.append(f"missing required field: {field_name(f)}")
    policy = record.get("execution_policy")
    if policy not in EXECUTION_POLICIES:
        errors.append(f"invalid execution_policy: {policy!r}")
    mat = record.get("materialization_policy", "UNKNOWN")
    if mat not in MATERIALIZATION_POLICIES:
        errors.append(f"invalid materialization_policy: {mat!r}")
    sched = record.get("scheduler_type")
    if sched is not None and sched not in ("cron", "systemd_system",
                                           "systemd_user", "manual",
                                           "retired", "unknown", "other"):
        errors.append(f"invalid scheduler_type: {sched!r}")
    cadence = record.get("cadence_seconds")
    if cadence is not None and (not isinstance(cadence, (int, float))
                                or cadence <= 0):
        errors.append("cadence_seconds must be positive number or null")
    # Impossible declarations:
    if policy in DORMANT_POLICIES and cadence is not None:
        errors.append(f"dormant policy {policy} must not declare a cadence")
    if policy == "PERIODIC" and cadence is None \
            and not record.get("multi_cadence"):
        vstat = str(record.get("verification_status",
                               "live_verified")).lower()
        if vstat not in ("unverified", "unknown", "placeholder"):
            errors.append("PERIODIC without cadence requires multi_cadence "
                          "or verification_status=unverified")
    return errors


def field_name(f: str) -> str:  # tiny helper keeping messages readable
    return f


def make_lane_config(**fields: Any) -> dict[str, Any]:
    record = {
        "schema_version": LANE_CONFIG_SPEC_VERSION,
        "environment": fields.pop("environment", "UNKNOWN"),
        "scheduler_type": fields.pop("scheduler_type", "unknown"),
        "unit_or_job": fields.pop("unit_or_job", "UNKNOWN"),
        "cadence_seconds": fields.pop("cadence_seconds", None),
        "multi_cadence": fields.pop("multi_cadence", False),
        "grace_multiplier": fields.pop("grace_multiplier", None),
        "materialization_policy": fields.pop("materialization_policy",
                                             "UNKNOWN"),
        "verification_status": fields.pop("verification_status",
                                          "live_verified"),
        "evidence_refs": fields.pop("evidence_refs", []),
        "active": fields.pop("active", True),
        "effective_end": fields.pop("effective_end", None),
        "notes": fields.pop("notes", ""),
        **fields,
    }
    errors = validate_config(record)
    if errors:
        raise ValueError("invalid lane config: " + "; ".join(errors))
    record["content_hash"] = content_hash(record)
    return record


def migrate_from_expectation(expectation: dict[str, Any]) -> dict[str, Any]:
    """Lossless migration from the v0.2-era expectations registry."""
    fields = {
        "expectation_id": expectation.get("expectation_id") or
                          expectation.get("config_id", ""),
        "clank_id": expectation.get("clank_id"),
        "instance_id": expectation.get("instance_id", "UNKNOWN"),
        "lane_id": expectation.get("lane_id", "UNKNOWN"),
        "execution_policy": expectation.get("policy"),
        "authority": expectation.get("authority", "UNKNOWN"),
        "cadence_seconds": expectation.get("cadence_seconds"),
        "multi_cadence": bool(expectation.get("multi_cadence")),
        "grace_multiplier": expectation.get("grace_multiplier"),
        "materialization_policy": expectation.get(
            "materialization_policy", "UNKNOWN"),
        "verification_status": expectation.get(
            "verification_status", "live_verified"),
        "active": expectation.get("active", True),
        "effective_end": expectation.get("effective_end"),
        "notes": expectation.get("notes", ""),
    }
    return make_lane_config(**fields)


def find_identity_conflicts(configs: list[dict[str, Any]]) -> list[str]:
    """One instance_id may belong to exactly one clank_id; one clank_id may
    hold an instance_id only once. Contradictory duplicates are conflicts."""
    owner: dict[str, tuple[str, str]] = {}
    conflicts: list[str] = []
    for c in configs:
        iid = c.get("instance_id")
        cid = c.get("clank_id")
        if iid == "UNKNOWN":
            continue
        key = f"{cid}/{c.get('lane_id', 'UNKNOWN')}"
        prev = owner.get(str(iid))
        if prev is None:
            owner[str(iid)] = (cid, key)
        elif prev[0] != cid:
            conflicts.append(
                f"instance {iid} claimed by both {prev[0]} and {cid}")
    return conflicts


def load_lane_configs(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    configs: list[dict[str, Any]] = []
    for lineno, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"lane-config:{lineno}: unparsable ({exc})")
            continue
        try:
            configs.append(make_lane_config(**rec))
        except ValueError as exc:
            warnings.append(f"lane-config:{lineno}: {exc}")
    return configs, warnings
