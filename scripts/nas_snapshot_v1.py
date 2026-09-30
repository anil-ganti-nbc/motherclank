"""Create governed, read-only NAS SQLite snapshots for ADR-0016.

Python 3.8 stdlib only. The input is an explicit JSON specification, never a
directory sweep. Example lane (all paths and revisions are operator evidence):

{
  "snapshot_contract_version": "1.0",
  "allowed_source_roots": ["/volume2/clank/oem-radar"],
  "lanes": [{
    "clank_id": "oem-radar", "instance_id": "nas-canonical", "lane_id": "default",
    "source_host": "Anil_NAS",
    "source_path": "/volume2/clank/oem-radar/canonical/state/radar.db",
    "snapshot_filename": "oem_radar.db",
    "child_as_of": {
      "query": "SELECT finished_at FROM crawler_runs ORDER BY id DESC LIMIT 1",
      "clock": "NATIVE_RUN_COMPLETED_AT", "naive_timezone": "UTC"
    },
    "schema_version": {"query": "SELECT COUNT(*) FROM schema_migrations"},
    "child_source_revision": "<source revision or null>",
    "child_source_revision_evidence": "<repository/host evidence or null>",
    "child_deployed_revision": "<deployed revision or null>",
    "child_deployed_revision_evidence": "<image/host evidence or null>",
    "unavailable_reasons": {},
    "freshness_horizon": {
      "max_age_seconds": 86400, "clock": "NATIVE_RUN_COMPLETED_AT",
      "source": "owner-approved lane policy"
    },
    "adapter_package_sha": "<40 lowercase hex>",
    "adapter_artifact_sha256": "<64 lowercase hex>",
    "adapter_package_version": "<version>",
    "observer_contract_version": "0.2"
  }]
}

Run with --spec SPEC.json --output-root EXISTING_DIRECTORY. Each invocation
creates one new, never reused directory under output-root. Mount that returned
directory read-only at /app/real-state in the candidate observer container.
The producer never opens a child store for writing, runs a collector, or uses
an old snapshot as current after a failed refresh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote


SNAPSHOT_CONTRACT_VERSION = "1.0"
OBSERVER_CONTRACT_VERSION = "0.2"
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SAFE_FILENAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*\.db$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024 * 1024


class SpecError(ValueError):
    """An unsafe, ambiguous, or incompatible snapshot specification."""


class SnapshotError(Exception):
    def __init__(self, code: str, integrity: Optional[Dict[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.integrity = integrity


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_string(obj: Dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecError("invalid_" + key)
    return value


def _absolute_path(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SpecError("invalid_" + key)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SpecError("unsafe_" + key)
    return path


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _query_spec(value: Any, key: str, *, clock: bool = False) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SpecError("invalid_" + key)
    sql = _required_string(value, "query").strip()
    if not re.match(r"^SELECT\s", sql, re.IGNORECASE) or ";" in sql:
        raise SpecError("unsafe_" + key + "_query")
    out = {"query": sql}
    if clock:
        out["clock"] = _required_string(value, "clock")
        naive_timezone = value.get("naive_timezone")
        if naive_timezone is not None:
            if naive_timezone != "UTC":
                raise SpecError("unsupported_naive_timezone")
            out["naive_timezone"] = "UTC"
    return out


def _validate_spec(spec: Any, output_root: Path, container_dir: str) -> List[Dict[str, Any]]:
    if not isinstance(spec, dict) or spec.get("snapshot_contract_version") != SNAPSHOT_CONTRACT_VERSION:
        raise SpecError("incompatible_snapshot_contract_version")
    if (not output_root.is_absolute() or not output_root.is_dir()
            or output_root.is_symlink() or str(output_root) == output_root.anchor):
        raise SpecError("unsafe_output_root")
    if not isinstance(container_dir, str) or not container_dir.startswith("/") or ".." in Path(container_dir).parts:
        raise SpecError("unsafe_container_dir")
    roots = spec.get("allowed_source_roots")
    if not isinstance(roots, list) or not roots:
        raise SpecError("missing_allowed_source_roots")
    allowed_roots = []
    for value in roots:
        root = _absolute_path(value, "allowed_source_root")
        if str(root) == root.anchor or root.is_symlink():
            raise SpecError("unsafe_allowed_source_root")
        allowed_roots.append(root.resolve())
    lanes = spec.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise SpecError("missing_lanes")
    seen = set()
    clank_ids = set()
    filenames = set()
    validated = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise SpecError("invalid_lane")
        for key in ("clank_id", "instance_id", "lane_id"):
            if not IDENTIFIER.fullmatch(_required_string(lane, key)):
                raise SpecError("unsafe_" + key)
        identity = (lane["clank_id"], lane["instance_id"], lane["lane_id"])
        if identity in seen:
            raise SpecError("duplicate_lane_identity")
        seen.add(identity)
        if lane["clank_id"] in clank_ids:
            raise SpecError("multiple_active_lanes_for_clank_unsupported")
        clank_ids.add(lane["clank_id"])
        filename = lane.get("snapshot_filename")
        if filename is None:
            filename = "__".join(identity) + ".db"
        if not isinstance(filename, str) or not SAFE_FILENAME.fullmatch(filename) or ".." in filename:
            raise SpecError("unsafe_snapshot_filename")
        if filename in filenames:
            raise SpecError("duplicate_snapshot_filename")
        filenames.add(filename)
        _required_string(lane, "source_host")
        source = _absolute_path(lane.get("source_path"), "source_path")
        # Resolve even when absent; this also catches symlinked ancestor escapes.
        resolved = source.resolve()
        if source.is_symlink() or not any(_under(resolved, root) for root in allowed_roots):
            raise SpecError("source_outside_allowed_roots")
        if _under(output_root.resolve(), source.parent.resolve()):
            raise SpecError("output_inside_child_state")
        as_of = _query_spec(lane.get("child_as_of"), "child_as_of", clock=True)
        schema = _query_spec(lane.get("schema_version"), "schema_version")
        reasons = lane.get("unavailable_reasons", {})
        if not isinstance(reasons, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip()
            for k, v in reasons.items()
        ):
            raise SpecError("invalid_unavailable_reasons")
        if as_of is None and not reasons.get("child_as_of"):
            raise SpecError("missing_child_as_of_reason")
        if schema is None and not reasons.get("schema_version"):
            raise SpecError("missing_schema_version_reason")
        for key in ("child_source_revision", "child_deployed_revision"):
            revision = lane.get(key)
            evidence = lane.get(key + "_evidence")
            if revision is None:
                if not reasons.get(key):
                    raise SpecError("missing_" + key + "_reason")
            elif not isinstance(revision, str) or not GIT_SHA.fullmatch(revision) or not isinstance(evidence, str) or not evidence.strip():
                raise SpecError("revision_without_evidence")
        horizon = lane.get("freshness_horizon")
        if not isinstance(horizon, dict):
            raise SpecError("invalid_freshness_horizon")
        max_age = horizon.get("max_age_seconds")
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not math.isfinite(max_age) or max_age <= 0:
            raise SpecError("invalid_max_age_seconds")
        _required_string(horizon, "clock")
        _required_string(horizon, "source")
        if as_of is not None and horizon["clock"] != as_of["clock"]:
            raise SpecError("horizon_clock_mismatch")
        if not GIT_SHA.fullmatch(_required_string(lane, "adapter_package_sha")):
            raise SpecError("invalid_adapter_package_sha")
        if not SHA256.fullmatch(_required_string(lane, "adapter_artifact_sha256")):
            raise SpecError("invalid_adapter_artifact_sha256")
        _required_string(lane, "adapter_package_version")
        if lane.get("observer_contract_version") != OBSERVER_CONTRACT_VERSION:
            raise SpecError("incompatible_observer_contract_version")
        last_good = lane.get("last_good_snapshot_ref")
        if last_good is not None and (
            not isinstance(last_good, str)
            or not SHA256.fullmatch(last_good[7:] if last_good.startswith("sha256:") else last_good)
        ):
            raise SpecError("invalid_last_good_snapshot_ref")
        history = lane.get("retained_prior_snapshot_refs", [])
        if not isinstance(history, list) or any(
            not isinstance(ref, str) or not SHA256.fullmatch(
                ref[7:] if ref.startswith("sha256:") else ref)
            for ref in history
        ):
            raise SpecError("invalid_retained_prior_snapshot_refs")
        copy = dict(lane)
        copy["_source_path"] = source
        copy["_resolved_source_path"] = resolved
        copy["_snapshot_filename"] = filename
        copy["_child_as_of_spec"] = as_of
        copy["_schema_version_spec"] = schema
        validated.append(copy)
    return validated


def _read_scalar(con: sqlite3.Connection, sql: str) -> Any:
    cursor = con.execute(sql)
    if len(cursor.description or ()) != 1:
        raise SnapshotError("METADATA_QUERY_SHAPE")
    row = cursor.fetchone()
    if cursor.fetchone() is not None:
        raise SnapshotError("METADATA_QUERY_SHAPE")
    return row[0] if row is not None else None


def _parse_native_time(value: Any, *, naive_timezone: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if naive_timezone != "UTC":
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _base_record(lane: Dict[str, Any], observed_at: str) -> Dict[str, Any]:
    return {
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "clank_id": lane["clank_id"],
        "instance_id": lane["instance_id"],
        "lane_id": lane["lane_id"],
        "source_host": lane["source_host"],
        "source_path": str(lane["_source_path"]),
        "snapshot_created_at": None,
        "child_as_of": None,
        "child_as_of_clock": None,
        "schema_version": None,
        "child_source_revision": lane.get("child_source_revision"),
        "child_source_revision_evidence": lane.get("child_source_revision_evidence"),
        "child_deployed_revision": lane.get("child_deployed_revision"),
        "child_deployed_revision_evidence": lane.get("child_deployed_revision_evidence"),
        "snapshot_path": None,
        "snapshot_host_path": None,
        "snapshot_sha256": None,
        "snapshot_bytes": None,
        "integrity_result": {"sqlite_integrity": "unavailable", "foreign_key_violations": None},
        "adapter_package_sha": lane["adapter_package_sha"],
        "adapter_artifact_sha256": lane["adapter_artifact_sha256"],
        "adapter_package_version": lane["adapter_package_version"],
        "observer_contract_version": OBSERVER_CONTRACT_VERSION,
        "refresh_outcome": "UNAVAILABLE",
        "freshness_state": "UNAVAILABLE",
        "child_execution_freshness": "UNKNOWN",
        "observed_at": observed_at,
        "freshness_horizon": dict(lane["freshness_horizon"]),
        "error_code": None,
        "last_good_snapshot_ref": lane.get("last_good_snapshot_ref"),
        "unavailable_reasons": dict(lane.get("unavailable_reasons", {})),
        "source_total_changes": None,
    }


def _mark_no_artifact(record: Dict[str, Any], reason: str) -> None:
    for key in ("snapshot_created_at", "snapshot_path", "snapshot_host_path", "snapshot_sha256", "snapshot_bytes"):
        record["unavailable_reasons"][key] = reason
    for key in ("child_as_of", "child_as_of_clock", "schema_version"):
        record["unavailable_reasons"][key] = reason
    if record["last_good_snapshot_ref"] is None:
        record["unavailable_reasons"]["last_good_snapshot_ref"] = "NO_CONFIGURED_LAST_GOOD_REFERENCE"


def _refresh_lane(lane: Dict[str, Any], run_dir: Path, container_dir: str,
                  diagnostics: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    observed_at = _iso_utc(_utc_now())
    record = _base_record(lane, observed_at)
    started = time.monotonic()
    diagnostic = {
        "clank_id": lane["clank_id"], "instance_id": lane["instance_id"],
        "lane_id": lane["lane_id"], "observed_at": observed_at,
        "failure_stage": None, "failure_operation": None,
        "exception_class": None, "sqlite_error_class": None,
        "sqlite_error_code": None, "sqlite_error_name": None,
        "destination_created": False, "backup_completed": False,
        "integrity_completed": False, "fk_completed": False,
        "completed_operations": [], "cleanup_errors": [],
        "configured_last_good_reference": lane.get("last_good_snapshot_ref"),
        "retained_prior_snapshot_refs": list(lane.get("retained_prior_snapshot_refs", [])),
        # Spec-supplied evidence only: absence of a reference is not proof
        # that no old snapshot exists. No sweep or automatic fallback.
        "retained_prior_snapshot_evidence": (
            "RETAINED_PRIOR_SNAPSHOT_NOT_ADMITTED_AS_FALLBACK"
            if lane.get("retained_prior_snapshot_refs") else
            "NO_RETAINED_PRIOR_SNAPSHOT" if lane.get("retained_prior_snapshot_refs") == [] else
            "RETAINED_PRIOR_SNAPSHOT_NOT_INSPECTED"
        ),
    }
    if diagnostics is not None:
        diagnostics.append(diagnostic)
    stage, operation = "OTHER", "SOURCE_PATH_CHECK"

    def failed(exc: Exception) -> None:
        diagnostic["failure_stage"] = stage
        diagnostic["failure_operation"] = operation
        diagnostic["exception_class"] = type(exc).__name__
        sql_exc = exc if isinstance(exc, sqlite3.Error) else exc.__cause__
        if isinstance(sql_exc, sqlite3.Error):
            diagnostic["sqlite_error_class"] = type(sql_exc).__name__
            code = getattr(sql_exc, "sqlite_errorcode", None)
            name = getattr(sql_exc, "sqlite_errorname", None)
            diagnostic["sqlite_error_code"] = code if type(code) is int else None
            diagnostic["sqlite_error_name"] = (
                name if isinstance(name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]+", name) else None
            )
        # Never serialize str(exc), SQL, paths from exceptions, or tracebacks.

    def finish() -> None:
        diagnostic["elapsed_seconds"] = round(time.monotonic() - started, 6)
        diagnostic["refresh_outcome"] = record["refresh_outcome"]
        diagnostic["error_code"] = record["error_code"]
    source_path = lane["_source_path"]
    if source_path.is_symlink() or source_path.resolve() != lane["_resolved_source_path"]:
        record["error_code"] = "SOURCE_PATH_CHANGED"
        record["refresh_outcome"] = "FAILED"
        record["freshness_state"] = "REFRESH_FAILED"
        _mark_no_artifact(record, "SOURCE_PATH_CHANGED")
        finish()
        return record
    if not source_path.is_file():
        record["error_code"] = "SOURCE_UNAVAILABLE"
        _mark_no_artifact(record, "SOURCE_UNAVAILABLE")
        finish()
        return record

    name = lane["_snapshot_filename"]
    temporary = run_dir / (name + ".partial")
    final = run_dir / name
    source_con = None
    try:
        stage, operation = "SOURCE_OPEN", "SOURCE_STAT"
        if source_path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("SOURCE_SIZE_LIMIT")
        uri = "file:" + quote(source_path.resolve().as_posix(), safe="/:") + "?mode=ro"
        stage, operation = "SOURCE_OPEN", "SOURCE_CONNECT"
        source_con = sqlite3.connect(uri, uri=True, timeout=10)
        diagnostic["completed_operations"].append(operation)
        stage, operation = "SOURCE_OPEN", "SOURCE_QUERY_ONLY"
        source_con.execute("PRAGMA query_only=ON")
        diagnostic["completed_operations"].append(operation)
        stage, operation = "DEST_CREATE", "DEST_CONNECT"
        with closing(sqlite3.connect(str(temporary), timeout=10)) as target_con:
            diagnostic["destination_created"] = temporary.is_file()
            diagnostic["completed_operations"].append(operation)
            stage, operation = "SQLITE_BACKUP", "BACKUP"
            source_con.backup(target_con)
            diagnostic["backup_completed"] = True
            diagnostic["completed_operations"].append(operation)
            stage, operation = "DEST_CREATE", "DEST_CLOSE"
        stage, operation = "SOURCE_OPEN", "SOURCE_NONMUTATION_CHECK"
        record["source_total_changes"] = source_con.total_changes
        if record["source_total_changes"] != 0:
            raise SnapshotError("SOURCE_NONMUTATION_VIOLATION")
        stage, operation = "SOURCE_OPEN", "SOURCE_CLOSE"
        source_con.close()
        source_con = None

        stage, operation = "DEST_CREATE", "COPY_CONNECT"
        with closing(sqlite3.connect(str(temporary), timeout=10)) as copy_con:
            stage, operation = "DEST_CREATE", "COPY_JOURNAL_DELETE"
            copy_con.execute("PRAGMA journal_mode=DELETE")
            stage, operation = "DEST_CREATE", "COPY_QUERY_ONLY"
            copy_con.execute("PRAGMA query_only=ON")
            stage, operation = "INTEGRITY_CHECK", "INTEGRITY_QUERY"
            integrity = copy_con.execute("PRAGMA integrity_check").fetchone()
            diagnostic["integrity_completed"] = True
            diagnostic["completed_operations"].append(operation)
            # A corrupt index diagnostic can include stored key values.
            # Retain the verdict, never its raw SQLite text.
            integrity_value = "ok" if integrity and integrity[0] == "ok" else "failed"
            record["integrity_result"]["sqlite_integrity"] = integrity_value
            if integrity_value != "ok":
                raise SnapshotError("SQLITE_INTEGRITY_FAILED", record["integrity_result"])
            stage, operation = "FK_CHECK", "FK_QUERY"
            fk_count = sum(1 for _ in copy_con.execute("PRAGMA foreign_key_check"))
            diagnostic["fk_completed"] = True
            diagnostic["completed_operations"].append(operation)
            record["integrity_result"] = {
                "sqlite_integrity": integrity_value,
                "foreign_key_violations": fk_count,
            }
            if fk_count:
                raise SnapshotError("FOREIGN_KEY_CHECK_FAILED", record["integrity_result"])
            as_of_spec = lane["_child_as_of_spec"]
            if as_of_spec is None:
                record["unavailable_reasons"]["child_as_of"] = lane["unavailable_reasons"]["child_as_of"]
                record["unavailable_reasons"]["child_as_of_clock"] = lane["unavailable_reasons"]["child_as_of"]
            else:
                stage, operation = "METADATA_READ", "AS_OF_QUERY"
                try:
                    native_value = _read_scalar(copy_con, as_of_spec["query"])
                except sqlite3.Error as exc:
                    raise SnapshotError("AS_OF_QUERY_FAILED") from exc
                record["child_as_of_clock"] = as_of_spec["clock"]
                native_time = _parse_native_time(
                    native_value, naive_timezone=as_of_spec.get("naive_timezone")
                )
                if native_time is None:
                    record["child_as_of_clock"] = None
                    record["unavailable_reasons"]["child_as_of"] = (
                        "NO_NATIVE_RUN" if native_value is None else "INVALID_NATIVE_TIMESTAMP"
                    )
                    record["unavailable_reasons"]["child_as_of_clock"] = record["unavailable_reasons"]["child_as_of"]
                else:
                    record["child_as_of"] = _iso_utc(native_time)
            schema_spec = lane["_schema_version_spec"]
            if schema_spec is None:
                record["unavailable_reasons"]["schema_version"] = lane["unavailable_reasons"]["schema_version"]
            else:
                stage, operation = "SCHEMA_READ", "SCHEMA_QUERY"
                try:
                    version = _read_scalar(copy_con, schema_spec["query"])
                except sqlite3.Error as exc:
                    raise SnapshotError("SCHEMA_QUERY_FAILED") from exc
                if version is None:
                    record["unavailable_reasons"]["schema_version"] = "NO_SCHEMA_VERSION_VALUE"
                elif not isinstance(version, (str, int)):
                    raise SnapshotError("INVALID_SCHEMA_VERSION_VALUE")
                else:
                    record["schema_version"] = str(version)
            stage, operation = "DEST_CREATE", "COPY_CLOSE"

        stage, operation = "HASH", "COPY_STAT"
        size = temporary.stat().st_size
        if size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("SNAPSHOT_SIZE_LIMIT")
        stage, operation = "HASH", "COPY_HASH"
        digest = _sha256(temporary)
        stage, operation = "PUBLICATION", "COPY_RENAME"
        os.replace(str(temporary), str(final))
        diagnostic["completed_operations"].append(operation)
        stage, operation = "METADATA_READ", "FRESHNESS_CALCULATION"
        created = _utc_now()
        record["snapshot_created_at"] = _iso_utc(created)
        record["snapshot_path"] = container_dir.rstrip("/") + "/" + name
        record["snapshot_host_path"] = str(final)
        record["snapshot_sha256"] = digest
        record["snapshot_bytes"] = size
        record["refresh_outcome"] = "SUCCESS"
        observation_time = _utc_now()
        record["observed_at"] = _iso_utc(observation_time)
        max_age = lane["freshness_horizon"]["max_age_seconds"]
        copy_age = (observation_time - created).total_seconds()
        record["freshness_state"] = "FRESH" if 0 <= copy_age <= max_age else "STALE"
        if record["child_as_of"] is not None:
            native = _parse_native_time(record["child_as_of"], naive_timezone=None)
            native_age = (observation_time - native).total_seconds()
            record["child_execution_freshness"] = (
                "UNKNOWN" if native_age < 0 else
                "FRESH" if native_age <= max_age else "STALE"
            )
        finish()
        return record
    except SnapshotError as exc:
        failed(exc)
        record["integrity_result"] = exc.integrity or record["integrity_result"]
        record["error_code"] = exc.code
    except sqlite3.Error as exc:
        failed(exc)
        record["error_code"] = "SQLITE_REFRESH_FAILED"
    except OSError as exc:
        failed(exc)
        record["error_code"] = "SNAPSHOT_IO_FAILED"
    except Exception as exc:
        failed(exc)
        # An unexpected implementation/source-shape error is still an
        # attributable failed attempt, never a healthy or omitted lane.
        record["error_code"] = "UNEXPECTED_REFRESH_FAILURE"
    finally:
        if source_con is not None:
            record["source_total_changes"] = source_con.total_changes
            try:
                source_con.close()
            except sqlite3.Error as exc:
                diagnostic["cleanup_errors"].append({"operation": "SOURCE_CLOSE", "exception_class": type(exc).__name__})
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError as exc:
                # It remains an unreferenced .partial, never current input.
                diagnostic["cleanup_errors"].append({"operation": "PARTIAL_UNLINK", "exception_class": type(exc).__name__})
    record["refresh_outcome"] = "FAILED"
    record["freshness_state"] = "REFRESH_FAILED"
    record["child_execution_freshness"] = "UNKNOWN"
    _mark_no_artifact(record, record["error_code"])
    finish()
    return record


def produce(spec: Dict[str, Any], output_root: Path, container_dir: str = "/app/real-state") -> Tuple[Path, Dict[str, Any]]:
    """Validate all lanes first; then produce one atomic manifest in a new dir."""
    output_root = Path(output_root)
    lanes = _validate_spec(spec, output_root, container_dir)
    run_dir = Path(tempfile.mkdtemp(prefix="snapshot-v1-", dir=str(output_root)))
    diagnostics: List[Dict[str, Any]] = []
    records = [_refresh_lane(lane, run_dir, container_dir, diagnostics) for lane in lanes]
    manifest = {
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "observed_at": _iso_utc(_utc_now()),
        "lanes": records,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    temporary = run_dir / "manifest.json.partial"
    final = run_dir / "manifest.json"
    with temporary.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(final))
    # Separate diagnostic artifact, NOT an additive manifest-v1.0 field.
    # Keep the normative snapshot contract and freshness semantics unchanged.
    with (run_dir / "refresh-diagnostics.json").open("x", encoding="utf-8") as stream:
        json.dump({"diagnostic_format_version": "1.0", "lanes": diagnostics}, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return final, manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--container-dir", default="/app/real-state")
    args = parser.parse_args(argv)
    try:
        with args.spec.open("r", encoding="utf-8") as stream:
            spec = json.load(stream)
        manifest_path, _ = produce(spec, args.output_root, args.container_dir)
    except (SpecError, OSError, json.JSONDecodeError) as exc:
        # Never print a path, SQL error, source payload, or spec contents.
        code = str(exc) if isinstance(exc, SpecError) else "SNAPSHOT_SETUP_FAILED"
        parser.exit(2, "snapshot_v1_error=" + code + "\n")
    print(str(manifest_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
