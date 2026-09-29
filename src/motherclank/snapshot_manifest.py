"""ADR-0016 snapshot-v1.0 intake for the NAS observer plane.

The manifest is a declaration about an independently created SQLite-safe
copy.  This module verifies that declaration against the *copy* before an
adapter can read it.  It never opens a child canonical store.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SNAPSHOT_CONTRACT_VERSION = "1.0"
OBSERVER_CONTRACT_VERSION = "0.2"
MAX_INTAKE_CLOCK_SKEW_SECONDS = 300

_REQUIRED = frozenset({
    "snapshot_contract_version", "clank_id", "instance_id", "lane_id",
    "source_host", "source_path", "snapshot_created_at", "child_as_of",
    "child_as_of_clock", "schema_version", "child_source_revision",
    "child_deployed_revision", "snapshot_path", "snapshot_sha256",
    "snapshot_bytes", "integrity_result", "adapter_package_sha",
    "adapter_artifact_sha256", "adapter_package_version",
    "observer_contract_version", "refresh_outcome", "freshness_state",
    "child_execution_freshness", "observed_at", "freshness_horizon",
    "error_code", "last_good_snapshot_ref",
})
_REFRESH = frozenset({"SUCCESS", "FAILED", "UNAVAILABLE"})
_FRESHNESS = frozenset({"FRESH", "STALE", "REFRESH_FAILED", "UNAVAILABLE"})
_EXECUTION = frozenset({"FRESH", "STALE", "UNKNOWN"})
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class SnapshotManifestError(ValueError):
    """Malformed or incompatible manifest: refuse the whole current input."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotManifestError(message)


def _timestamp(value: Any, label: str) -> datetime:
    _require(isinstance(value, str) and bool(value.strip()),
             f"{label}: UTC timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotManifestError(f"{label}: malformed timestamp") from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
             f"{label}: timestamp must be UTC")
    return parsed.astimezone(UTC)


def _sha256(value: Any, label: str) -> str:
    _require(isinstance(value, str), f"{label}: SHA-256 required")
    match = _SHA256.fullmatch(value)
    _require(match is not None, f"{label}: malformed SHA-256")
    return match.group(1).lower()


def _nonempty(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()),
             f"{label}: nonempty string required")
    return value


def _null_reason(record: dict[str, Any], field: str, label: str) -> None:
    if record[field] is not None:
        return
    reasons = record.get("unavailable_reasons")
    _require(isinstance(reasons, dict)
             and isinstance(reasons.get(field), str)
             and bool(reasons[field].strip()),
             f"{label}.{field}: null requires unavailable_reasons entry")


def _validate_record(record: Any, position: int) -> dict[str, Any]:
    label = f"lanes[{position}]"
    _require(isinstance(record, dict), f"{label}: mapping required")
    missing = _REQUIRED - record.keys()
    _require(not missing, f"{label}: missing fields {sorted(missing)}")
    _require(record["snapshot_contract_version"] == SNAPSHOT_CONTRACT_VERSION,
             f"{label}: incompatible snapshot contract version")
    _require(record["observer_contract_version"] == OBSERVER_CONTRACT_VERSION,
             f"{label}: incompatible observer contract version")
    for key in ("clank_id", "instance_id", "lane_id", "source_host",
                "adapter_package_version"):
        _nonempty(record[key], f"{label}.{key}")
    source = Path(_nonempty(record["source_path"], f"{label}.source_path"))
    _require(source.is_absolute(), f"{label}.source_path: absolute path required")
    _require(isinstance(record["adapter_package_sha"], str)
             and _SHA40.fullmatch(record["adapter_package_sha"]) is not None,
             f"{label}.adapter_package_sha: Git SHA required")
    _sha256(record["adapter_artifact_sha256"],
            f"{label}.adapter_artifact_sha256")
    for key, allowed in (("refresh_outcome", _REFRESH),
                         ("freshness_state", _FRESHNESS),
                         ("child_execution_freshness", _EXECUTION)):
        _require(record[key] in allowed, f"{label}.{key}: invalid state")
    observed = _timestamp(record["observed_at"], f"{label}.observed_at")
    horizon = record["freshness_horizon"]
    _require(isinstance(horizon, dict),
             f"{label}.freshness_horizon: mapping required")
    seconds = horizon.get("max_age_seconds")
    _require(isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
             and math.isfinite(seconds) and seconds > 0,
             f"{label}.freshness_horizon.max_age_seconds: positive number required")
    _nonempty(horizon.get("clock"), f"{label}.freshness_horizon.clock")
    _nonempty(horizon.get("source"), f"{label}.freshness_horizon.source")
    for key in ("child_as_of", "child_as_of_clock", "schema_version",
                "child_source_revision", "child_deployed_revision"):
        _null_reason(record, key, label)
    if record["schema_version"] is not None:
        version = record["schema_version"]
        _require(isinstance(version, (str, int)) and not isinstance(version, bool)
                 and bool(str(version).strip()),
                 f"{label}.schema_version: nonempty string/integer or reasoned null required")
    for key in ("child_source_revision", "child_deployed_revision"):
        if record[key] is not None:
            _require(isinstance(record[key], str)
                     and _SHA40.fullmatch(record[key]) is not None,
                     f"{label}.{key}: Git SHA or reasoned null required")
    if record["child_as_of"] is None:
        _require(record["child_as_of_clock"] is None
                 and record["child_execution_freshness"] == "UNKNOWN",
                 f"{label}: absent child clock cannot claim fresh execution")
    else:
        _nonempty(record["child_as_of_clock"], f"{label}.child_as_of_clock")
        child_as_of = _timestamp(record["child_as_of"], f"{label}.child_as_of")
        if record["child_execution_freshness"] == "FRESH":
            child_age = (observed - child_as_of).total_seconds()
            _require(0 <= child_age <= seconds,
                     f"{label}: stale/future child execution cannot be FRESH")
    outcome = record["refresh_outcome"]
    freshness = record["freshness_state"]
    if outcome == "SUCCESS":
        _require(freshness in ("FRESH", "STALE"),
                 f"{label}: SUCCESS needs FRESH or STALE copy")
        if "source_total_changes" in record:
            _require(type(record["source_total_changes"]) is int
                     and record["source_total_changes"] == 0,
                     f"{label}: observer-side source writes cannot be current")
        path = Path(_nonempty(record["snapshot_path"],
                              f"{label}.snapshot_path"))
        _require(path.is_absolute() and path != source,
                 f"{label}.snapshot_path: distinct absolute copy path required")
        _sha256(record["snapshot_sha256"], f"{label}.snapshot_sha256")
        _require(isinstance(record["snapshot_bytes"], int)
                 and not isinstance(record["snapshot_bytes"], bool)
                 and record["snapshot_bytes"] > 0,
                 f"{label}.snapshot_bytes: positive integer required")
        created = _timestamp(record["snapshot_created_at"],
                             f"{label}.snapshot_created_at")
        _require(created <= observed,
                 f"{label}: copy completion after observation")
        if record["child_as_of"] is not None:
            _require(_timestamp(record["child_as_of"],
                                f"{label}.child_as_of") <= created,
                     f"{label}: child execution after snapshot completion")
        if freshness == "FRESH":
            _require((observed - created).total_seconds() <= seconds,
                     f"{label}: stale copy cannot be FRESH")
        integrity = record["integrity_result"]
        _require(isinstance(integrity, dict)
                 and integrity.get("sqlite_integrity") == "ok"
                 and integrity.get("foreign_key_violations") == 0,
                 f"{label}: SUCCESS requires SQLite integrity ok and FK 0")
        _require(record["error_code"] is None,
                 f"{label}: SUCCESS cannot carry error_code")
    else:
        _require(freshness == ("REFRESH_FAILED" if outcome == "FAILED"
                               else "UNAVAILABLE"),
                 f"{label}: failure and freshness states disagree")
        _require(all(record[key] is None for key in
                     ("snapshot_path", "snapshot_sha256", "snapshot_bytes")),
                 f"{label}: failed/unavailable attempt cannot present a current copy")
        _nonempty(record["error_code"], f"{label}.error_code")
        if record["snapshot_created_at"] is None:
            _null_reason(record, "snapshot_created_at", label)
    if record["last_good_snapshot_ref"] is not None:
        _sha256(record["last_good_snapshot_ref"],
                f"{label}.last_good_snapshot_ref")
    return record


def load_manifest(path: Path) -> dict[str, Any]:
    """Validate the envelope and each attempted lane before adapter probing."""
    try:
        _require(not path.is_symlink() and path.is_file(),
                 f"manifest missing or symlinked: {path}")
        manifest_bytes = path.read_bytes()
        doc = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotManifestError(f"manifest unreadable or malformed: {path}") from exc
    _require(isinstance(doc, dict), "manifest: mapping required")
    _require(doc.get("snapshot_contract_version") == SNAPSHOT_CONTRACT_VERSION,
             "manifest: incompatible snapshot contract version")
    _timestamp(doc.get("observed_at"), "manifest.observed_at")
    lanes = doc.get("lanes")
    _require(isinstance(lanes, list), "manifest.lanes: list required")
    seen_keys: set[tuple[str, str, str]] = set()
    seen_clanks: set[str] = set()
    for position, raw in enumerate(lanes):
        row = _validate_record(raw, position)
        key = (row["clank_id"], row["instance_id"], row["lane_id"])
        _require(key not in seen_keys, f"manifest: duplicate lane {key}")
        _require(row["clank_id"] not in seen_clanks,
                 f"manifest: multiple active lanes for {row['clank_id']} unsupported")
        seen_keys.add(key)
        seen_clanks.add(row["clank_id"])
    doc["_verified_manifest_sha256"] = "sha256:" + hashlib.sha256(
        manifest_bytes).hexdigest()
    return doc


def verify_adapter_identity(doc: dict[str, Any], source_sha: str | None,
                            artifact_sha256: str | None) -> None:
    """Require launcher-attested executable identities, not self-assertion."""
    _require(isinstance(source_sha, str) and _SHA40.fullmatch(source_sha) is not None,
             "expected adapter package Git SHA required for v1 intake")
    digest = _sha256(artifact_sha256, "expected adapter artifact SHA-256")
    for row in doc["lanes"]:
        _require(row["adapter_package_sha"].lower() == source_sha.lower(),
                 f"{row['clank_id']}: adapter source SHA does not match runtime")
        _require(_sha256(row["adapter_artifact_sha256"],
                         f"{row['clank_id']}.adapter_artifact_sha256") == digest,
                 f"{row['clank_id']}: adapter artifact digest does not match runtime")


def utc_now() -> datetime:
    """Intake clock, kept separate so replay tests can freeze it."""
    return datetime.now(UTC)


def assess_intake(doc: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Re-evaluate freshness at consumption, never trust a past FRESH label.

    A producer's FRESH labels describe its *attempt time*. The same manifest
    cannot be replayed indefinitely as current input. This check is monotonic:
    it may downgrade producer freshness but never upgrade it. Bounded clock
    skew allows a NAS host/container clock difference without accepting an
    arbitrarily future-dated snapshot or native child run.
    """
    now = utc_now().astimezone(UTC)
    future_limit = now + timedelta(seconds=MAX_INTAKE_CLOCK_SKEW_SECONDS)
    _require(_timestamp(doc["observed_at"], "manifest.observed_at") <= future_limit,
             "manifest.observed_at: future beyond intake clock-skew bound")
    assessed: dict[str, dict[str, Any]] = {}
    for row in doc["lanes"]:
        label = row["clank_id"]
        _require(_timestamp(row["observed_at"], f"{label}.observed_at")
                 <= future_limit,
                 f"{label}: producer observation future beyond intake clock-skew bound")
        for key in ("snapshot_created_at", "child_as_of"):
            if row[key] is not None:
                _require(_timestamp(row[key], f"{label}.{key}") <= future_limit,
                         f"{label}: {key} future beyond intake clock-skew bound")

        copy_state = row["freshness_state"]
        child_state = row["child_execution_freshness"]
        reasons: list[str] = []
        if row["refresh_outcome"] == "SUCCESS":
            horizon = row["freshness_horizon"]["max_age_seconds"]
            copy_age = (now - _timestamp(row["snapshot_created_at"],
                                         f"{label}.snapshot_created_at")).total_seconds()
            if copy_age > horizon:
                copy_state = "STALE"
                reasons.append("COPY_AGE_EXCEEDS_HORIZON_AT_INTAKE")
            elif copy_state != "FRESH":
                reasons.append("PRODUCER_MARKED_COPY_STALE")
            if row["child_as_of"] is None:
                child_state = "UNKNOWN"
                reasons.append("NO_NATIVE_CHILD_AS_OF")
            else:
                child_age = (now - _timestamp(row["child_as_of"],
                                              f"{label}.child_as_of")).total_seconds()
                if child_age > horizon:
                    child_state = "STALE"
                    reasons.append("CHILD_AGE_EXCEEDS_HORIZON_AT_INTAKE")
                elif child_state != "FRESH":
                    reasons.append("PRODUCER_MARKED_CHILD_NOT_FRESH")
        else:
            reasons.append(row["error_code"])
        # Copy/child clocks alone never establish semantic compatibility.
        # A successful adapter/schema check in build_snapshot is the only
        # transition from UNVERIFIED to effective FRESH.
        effective = ("STALE" if "STALE" in (copy_state, child_state)
                     else "UNVERIFIED" if row["refresh_outcome"] == "SUCCESS"
                     and copy_state == "FRESH" and child_state == "FRESH"
                     else "UNKNOWN")
        assessed[label] = {
            "intake_observed_at": now.isoformat(),
            "intake_freshness_state": copy_state,
            "intake_child_execution_freshness": child_state,
            "intake_reasons": reasons,
            "effective_freshness_state": effective,
        }
    return now.isoformat(), assessed


def verify_copy(record: dict[str, Any], adapter_path: Path) -> None:
    """Verify actual immutable copy identity and SQLite checks, never source."""
    label = record["clank_id"]
    declared = Path(record["snapshot_path"])
    def reject_sidecars() -> None:
        # A hash of the main database does not cover committed WAL pages.
        # A mode=ro adapter would otherwise read un-hashed rows from -wal.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(declared) + suffix)
            _require(not (sidecar.exists() or sidecar.is_symlink()),
                     f"{label}: copy SQLite sidecar {suffix} present")

    _require(declared == adapter_path,
             f"{label}: manifest copy path does not match adapter DB path")
    _require(not declared.is_symlink() and declared.is_file(),
             f"{label}: copy missing or symlinked")
    reject_sidecars()
    _require(declared.stat().st_size == record["snapshot_bytes"],
             f"{label}: copy byte count mismatch")
    digest = hashlib.sha256()
    with declared.open("rb") as stream:
        header = stream.read(20)
        digest.update(header)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    # SQLite's immutable=1 connection can report DELETE even for a WAL-mode
    # main-file header with no sidecar. The persistent header bytes are the
    # independent check that future mode=ro adapters cannot see WAL pages.
    _require(len(header) == 20 and header[:16] == b"SQLite format 3\0"
             and header[18:20] == b"\x01\x01",
             f"{label}: immutable copy header journal_mode is not delete")
    _require(digest.hexdigest() == _sha256(record["snapshot_sha256"],
                                          f"{label}.snapshot_sha256"),
             f"{label}: copy SHA-256 mismatch")
    try:
        db = sqlite3.connect(
            f"file:{declared.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            fk_count = len(db.execute("PRAGMA foreign_key_check").fetchall())
            _require(db.total_changes == 0,
                     f"{label}: copy connection unexpectedly wrote data")
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise SnapshotManifestError(f"{label}: SQLite copy unreadable") from exc
    _require(integrity == "ok" and fk_count == 0,
             f"{label}: SQLite copy integrity/FK mismatch")
    _require(journal_mode == "delete",
             f"{label}: immutable copy journal_mode is not delete")
    reject_sidecars()


def lineage(record: dict[str, Any]) -> dict[str, Any]:
    """Small, secret-free provenance that every derived consumer can cite."""
    return {key: record[key] for key in (
        "snapshot_contract_version", "clank_id", "instance_id", "lane_id",
        "source_host", "source_path", "snapshot_created_at", "child_as_of",
        "child_as_of_clock", "schema_version", "child_source_revision",
        "child_deployed_revision", "snapshot_path", "snapshot_sha256",
        "snapshot_bytes", "adapter_package_sha", "adapter_artifact_sha256",
        "adapter_package_version", "observer_contract_version",
        "refresh_outcome", "freshness_state", "child_execution_freshness",
        "observed_at", "freshness_horizon", "error_code",
        "last_good_snapshot_ref",
    )}
