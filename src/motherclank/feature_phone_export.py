"""Translate a sealed child export into ADR-0016 snapshot-manifest v1.0.

This is a transport consumer, not an exporter or a child-domain adapter. It
has no process-control, publisher, canonical-store access or domain SQL. Production
intake requires a non-root Linux process and the accepted-only kernel-RO bind.
The child-owned host helper's receipt is bound to an independently supplied
request context; a previous publication cannot become a new refresh SUCCESS.
"""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import snapshot_manifest as manifest_v1

EXPORT_FORMAT_VERSION = "1.0"
PUBLISHER_VERSION = "1.0"
REQUEST_FORMAT_VERSION = "1.0"
EXPORTER_REVISION = "99405de0829e5baade4151eb1c2a0f2374b49613"
EXPORT_IMAGE = "sha256:eccdb14e96634a255ce37e6ea9b9391997c8845710fe3fd8cea52129c3612478"
DEPLOYED_REVISION = "3e6e19a2d5b7cb5004aab12145d099521a200c25"
CLANK_ID = "feature-phone-clank"
INSTANCE_ID = "feature-phone-clank-nas-cops-000072"
LANE_ID = "experimental"
SOURCE_HOST = "Anil_NAS"
CANONICAL_SOURCE_PATH = "/volume2/clank/feature-phone-clank/state/feature_phone_clank.db"
PUBLICATION_HOST_ROOT = "/volume2/clank/feature-phone-clank/observer-export/accepted"
ACCEPTED_ROOT = Path("/app/feature-phone-accepted")
DB_NAME = "feature_phone_clank.db"
META_NAME = "metadata.json"
SEAL_NAME = "publication.json"
FILES = frozenset({DB_NAME, META_NAME, SEAL_NAME})
ATTEMPT_ID = re.compile(r"export-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
ERROR_CODE = re.compile(r"[A-Z0-9_]{1,80}\Z")
ERROR_STAGES = frozenset({"ADMISSION", "EXPORT", "PUBLISH", "VERIFY", "RETENTION", "LOCK"})
HORIZON = {"max_age_seconds": 36000, "clock": "NATIVE_RUN_ROW_UTC",
           "source": "owner-approved Feature Phone NAS experimental lane policy"}
RECEIPT_KEYS = frozenset({
    "request_format_version", "status", "request_id", "request_started_at",
    "request_completed_at", "attempt_id", "exporter_revision", "export_image",
    "deployed_revision", "artifact_sha256", "metadata_sha256", "publication_sha256",
    "snapshot_bytes", "publication_path", "error_code", "error_stage",
    "last_good_snapshot_ref",
})
LINEAGE_KEYS = frozenset({
    "request_format_version", "status", "request_id", "request_started_at", "request_completed_at",
    "attempt_id", "exporter_revision", "export_image", "artifact_sha256", "metadata_sha256",
    "publication_sha256", "publication_path", "error_stage", "export_format_version",
})
METADATA_KEYS = frozenset({
    "status", "export_format_version", "publisher_version", "clank_id", "instance_id",
    "lane_id", "canonical_source_path", "deployed_revision", "exporter_revision", "source_revision",
    "source_revision_unavailable_reason", "schema_version", "child_as_of", "child_as_of_clock",
    "child_as_of_unavailable_reason", "export_completed_at", "artifact_filename", "artifact_path",
    "size_bytes", "sha256", "integrity_check", "foreign_key_violations", "sqlite_runtime_version",
    "source_access",
})
SEAL_KEYS = frozenset({
    "status", "publisher_version", "export_format_version", "attempt_id",
    "metadata_sha256", "artifact_sha256", "exporter_revision", "deployed_revision", "published_at",
})


class FeaturePhoneExportError(manifest_v1.SnapshotManifestError):
    """A bounded transport code only; untrusted paths/errors never cross it."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise FeaturePhoneExportError(code)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: Any) -> datetime:
    _require(type(value) is str and 0 < len(value) <= 40, "INVALID_UTC_TIMESTAMP")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FeaturePhoneExportError("INVALID_UTC_TIMESTAMP") from None
    _require(stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0,
             "INVALID_UTC_TIMESTAMP")
    return stamp.astimezone(UTC)


def _sha(value: Any) -> str:
    _require(type(value) is str, "INVALID_SHA256")
    bare = value[7:] if value.startswith("sha256:") else value
    _require(SHA64.fullmatch(bare) is not None, "INVALID_SHA256")
    return bare


def _last_good(value: Any) -> str | None:
    return None if value is None else _sha(value)


def _exact(obj: dict, key: str, expected: Any, code: str) -> None:
    _require(obj.get(key) == expected and type(obj.get(key)) is type(expected), code)


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "DUPLICATE_JSON_KEY")
            value[key] = item
        return value
    try:
        _require(path.stat().st_size <= 32768, "TRANSPORT_METADATA_TOO_LARGE")
        raw = path.read_bytes()
        _require(len(raw) <= 32768, "TRANSPORT_METADATA_TOO_LARGE")
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise FeaturePhoneExportError("TRANSPORT_METADATA_UNREADABLE") from None
    _require(type(doc) is dict, "TRANSPORT_METADATA_INVALID")
    return doc, hashlib.sha256(raw).hexdigest()


def _check_stat(info, *, directory: bool) -> None:
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid == 0 and info.st_gid == 10001
             and stat.S_IMODE(info.st_mode) == (0o550 if directory else 0o440)
             and (directory or info.st_nlink == 1), "PUBLICATION_NOT_ROOT_SEALED")


def _sealed_boundary(root: Path, attempt: Path) -> None:
    """No production opt-out: fixtures mock this function, never caller flags."""
    _require(os.name == "posix" and os.geteuid() == 10001 and os.getegid() == 10001,
             "LINUX_NONROOT_CONSUMER_REQUIRED")
    try:
        status_text = Path("/proc/self/status").read_text(encoding="utf-8")
        caps = re.search(r"^CapEff:\s*([0-9a-fA-F]+)$", status_text, re.M)
        _require(caps is not None and int(caps.group(1), 16) == 0,
                 "CONSUMER_CAPABILITIES_NOT_DROPPED")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        _require(libc.prctl(39, 0, 0, 0, 0) == 1, "CONSUMER_NO_NEW_PRIVILEGES_REQUIRED")
        for path in (root, *root.parents):
            _require(not path.is_symlink(), "SYMLINKED_ACCEPTED_BIND")
        _check_stat(root.lstat(), directory=True)
        _check_stat(attempt.lstat(), directory=True)
        _require({p.name for p in attempt.iterdir()} == FILES,
                 "PUBLICATION_LAYOUT_INVALID")
        for name in FILES:
            _check_stat((attempt / name).lstat(), directory=False)
    except (OSError, AttributeError):
        raise FeaturePhoneExportError("PUBLICATION_UNAVAILABLE") from None


def _kernel_readonly(root: Path) -> None:
    """Require the exact accepted-only bind, not merely SQLite mode=ro."""
    _require(os.name == "posix", "LINUX_READONLY_BIND_REQUIRED")
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        raise FeaturePhoneExportError("READONLY_BIND_NOT_EVIDENCED") from None
    matches = []
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 10 and "-" in fields:
            mount_path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
            if mount_path == root.as_posix():
                matches.append(fields[5].split(","))
            elif mount_path.startswith(root.as_posix() + "/"):
                raise FeaturePhoneExportError("NESTED_ACCEPTED_MOUNT_NOT_ALLOWED")
    _require(len(matches) == 1 and "ro" in matches[0] and "rw" not in matches[0],
             "ACCEPTED_BIND_NOT_KERNEL_READONLY")


def _metadata(m: dict[str, Any]) -> None:
    _require(set(m) == METADATA_KEYS, "EXPORT_METADATA_CONTRACT_INVALID")
    for key, value in {
        "status": "VERIFIED_PRIVATE", "export_format_version": EXPORT_FORMAT_VERSION,
        "publisher_version": PUBLISHER_VERSION, "clank_id": CLANK_ID,
        "instance_id": INSTANCE_ID, "lane_id": LANE_ID,
        "canonical_source_path": CANONICAL_SOURCE_PATH, "deployed_revision": DEPLOYED_REVISION,
        "exporter_revision": EXPORTER_REVISION, "source_revision": None,
        "source_revision_unavailable_reason": "NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE",
        "schema_version": 7, "artifact_filename": DB_NAME, "artifact_path": DB_NAME,
        "integrity_check": "ok", "foreign_key_violations": 0,
    }.items():
        _exact(m, key, value, "EXPORT_METADATA_BINDING_INVALID")
    _sha(m["sha256"])
    _require(type(m["size_bytes"]) is int and 0 < m["size_bytes"] <= 1024**3,
             "EXPORT_METADATA_SIZE_INVALID")
    _require(type(m["sqlite_runtime_version"]) is str
             and re.fullmatch(r"\d+\.\d+\.\d+", m["sqlite_runtime_version"]) is not None,
             "EXPORT_METADATA_RUNTIME_INVALID")
    _utc(m["export_completed_at"])
    if m["child_as_of"] is None:
        _require(m["child_as_of_clock"] == "UNAVAILABLE"
                 and type(m["child_as_of_unavailable_reason"]) is str
                 and m["child_as_of_unavailable_reason"] in {
                     "NO_NATIVE_RUN_CLOCK", "NATIVE_CLOCK_NOT_EXPLICIT_UTC", "NATIVE_CLOCK_INVALID"},
                 "EXPORT_NATIVE_CLOCK_INVALID")
    else:
        _require(m["child_as_of_clock"] == "NATIVE_RUN_ROW_UTC"
                 and m["child_as_of_unavailable_reason"] is None
                 and _utc(m["child_as_of"]) <= _utc(m["export_completed_at"]),
                 "EXPORT_NATIVE_CLOCK_INVALID")
    access = m["source_access"]
    _require(type(access) is dict and set(access) == {
        "mode", "query_only", "authorizer", "total_changes", "copy_total_changes",
        "schema_cookie_unchanged", "sidecars_before", "sidecars_after", "sidecar_authority"},
        "EXPORT_SOURCE_PROOF_INVALID")
    for key, value in {"mode": "ro", "query_only": True, "authorizer": "READ_ALLOWLIST_V1",
                       "total_changes": 0, "copy_total_changes": 0, "schema_cookie_unchanged": True,
                       "sidecar_authority": "CHILD_SQLITE_COORDINATION_NOT_DOMAIN_WRITES"}.items():
        _exact(access, key, value, "EXPORT_SOURCE_PROOF_INVALID")
    for key in ("sidecars_before", "sidecars_after"):
        _require(type(access[key]) is dict and set(access[key]) == {"-wal", "-shm", "-journal"}
                 and all(type(v) is bool for v in access[key].values()), "EXPORT_SOURCE_PROOF_INVALID")


def _receipt(receipt: dict, *, request_id: str, request_started_at: str,
             observed_at: str, last_good_snapshot_ref: str | None) -> tuple[datetime, datetime, datetime]:
    _require(type(receipt) is dict and set(receipt) == RECEIPT_KEYS,
             "REQUEST_RECEIPT_CONTRACT_INVALID")
    _require(type(request_id) is str and REQUEST_ID.fullmatch(request_id) is not None
             and ".." not in request_id, "REQUEST_ID_INVALID")
    for key, value in {"request_format_version": REQUEST_FORMAT_VERSION, "request_id": request_id,
                       "request_started_at": request_started_at, "exporter_revision": EXPORTER_REVISION,
                       "export_image": EXPORT_IMAGE, "deployed_revision": DEPLOYED_REVISION}.items():
        _exact(receipt, key, value, "REQUEST_RECEIPT_BINDING_INVALID")
    _require(type(receipt["status"]) is str and receipt["status"] in {"SUCCESS", "FAILED", "UNAVAILABLE"},
             "REQUEST_RECEIPT_STATUS_INVALID")
    start, end, observed = map(_utc, (request_started_at, receipt["request_completed_at"], observed_at))
    _require(start <= end <= observed <= utc_now(), "REQUEST_RECEIPT_TIME_INVALID")
    _require(_last_good(receipt["last_good_snapshot_ref"]) == _last_good(last_good_snapshot_ref),
             "LAST_GOOD_REFERENCE_BINDING_INVALID")
    return start, end, observed


def _base(*, adapter_package_sha: str, adapter_artifact_sha256: str,
          adapter_package_version: str, observed_at: str, last_good_snapshot_ref: str | None) -> dict:
    _require(type(adapter_package_sha) is str and SHA40.fullmatch(adapter_package_sha) is not None,
             "ADAPTER_PACKAGE_SHA_INVALID")
    _sha(adapter_artifact_sha256)
    _require(type(adapter_package_version) is str and 0 < len(adapter_package_version) <= 80
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", adapter_package_version) is not None,
             "ADAPTER_PACKAGE_VERSION_INVALID")
    return {
        "snapshot_contract_version": manifest_v1.SNAPSHOT_CONTRACT_VERSION,
        "clank_id": CLANK_ID, "instance_id": INSTANCE_ID, "lane_id": LANE_ID,
        "source_host": SOURCE_HOST, "source_path": CANONICAL_SOURCE_PATH,
        "snapshot_created_at": None, "child_as_of": None, "child_as_of_clock": None,
        "schema_version": None, "child_source_revision": None,
        "child_deployed_revision": DEPLOYED_REVISION, "snapshot_path": None,
        "snapshot_sha256": None, "snapshot_bytes": None,
        "integrity_result": {"sqlite_integrity": "unavailable", "foreign_key_violations": None},
        "adapter_package_sha": adapter_package_sha, "adapter_artifact_sha256": adapter_artifact_sha256,
        "adapter_package_version": adapter_package_version,
        "observer_contract_version": manifest_v1.OBSERVER_CONTRACT_VERSION,
        "refresh_outcome": "UNAVAILABLE", "freshness_state": "UNAVAILABLE",
        "child_execution_freshness": "UNKNOWN", "observed_at": observed_at,
        "freshness_horizon": dict(HORIZON), "error_code": None,
        "last_good_snapshot_ref": _last_good(last_good_snapshot_ref),
        "unavailable_reasons": {"child_source_revision": "NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE"},
    }


def translate_failure(receipt: dict, *, request_id: str, request_started_at: str,
                      observed_at: str, adapter_package_sha: str, adapter_artifact_sha256: str,
                      adapter_package_version: str, last_good_snapshot_ref: str | None = None,
                      prior_attempt_id: str | None = None) -> dict:
    """A failed request produces no current copy and never opens old evidence."""
    _receipt(receipt, request_id=request_id, request_started_at=request_started_at,
             observed_at=observed_at, last_good_snapshot_ref=last_good_snapshot_ref)
    _require(prior_attempt_id is None or (type(prior_attempt_id) is str
             and ATTEMPT_ID.fullmatch(prior_attempt_id) is not None), "PRIOR_ATTEMPT_ID_INVALID")
    _require(type(receipt["status"]) is str and receipt["status"] in {"FAILED", "UNAVAILABLE"}, "FAILURE_RECEIPT_REQUIRED")
    _require(all(receipt[k] is None for k in ("attempt_id", "artifact_sha256", "metadata_sha256",
                                             "publication_sha256", "snapshot_bytes", "publication_path")),
             "FAILED_REQUEST_HAS_CURRENT_ARTIFACT")
    _require(type(receipt["error_code"]) is str and ERROR_CODE.fullmatch(receipt["error_code"]) is not None
             and type(receipt["error_stage"]) is str and receipt["error_stage"] in ERROR_STAGES,
             "FAILURE_DIAGNOSTIC_INVALID")
    row = _base(adapter_package_sha=adapter_package_sha, adapter_artifact_sha256=adapter_artifact_sha256,
                adapter_package_version=adapter_package_version, observed_at=observed_at,
                last_good_snapshot_ref=last_good_snapshot_ref)
    row.update(refresh_outcome=receipt["status"],
               freshness_state="REFRESH_FAILED" if receipt["status"] == "FAILED" else "UNAVAILABLE",
               error_code=receipt["error_code"])
    row["unavailable_reasons"].update({k: "NO_CURRENT_EXPORT_AFTER_" + receipt["error_stage"]
                                      for k in ("snapshot_created_at", "schema_version", "child_as_of", "child_as_of_clock")})
    row["child_export"] = _provenance(receipt)
    manifest_v1._validate_record(row, 0)
    return row


def _provenance(receipt: dict) -> dict:
    return {key: receipt[key] for key in (
        "request_format_version", "status", "request_id", "request_started_at", "request_completed_at",
        "attempt_id", "exporter_revision", "export_image", "artifact_sha256", "metadata_sha256",
        "publication_sha256", "publication_path", "error_stage") } | {"export_format_version": EXPORT_FORMAT_VERSION}


def validate_lineage(value: Any, *, record: dict | None = None) -> dict:
    """Bounded additive export provenance; not a third normative wire contract."""
    _require(type(value) is dict and set(value) == LINEAGE_KEYS, "EXPORT_LINEAGE_INVALID")
    for key, expected in {"request_format_version": REQUEST_FORMAT_VERSION,
                          "export_format_version": EXPORT_FORMAT_VERSION,
                          "exporter_revision": EXPORTER_REVISION, "export_image": EXPORT_IMAGE}.items():
        _exact(value, key, expected, "EXPORT_LINEAGE_INVALID")
    _require(type(value["request_id"]) is str and REQUEST_ID.fullmatch(value["request_id"]) is not None
             and ".." not in value["request_id"], "EXPORT_LINEAGE_INVALID")
    _require(_utc(value["request_started_at"]) <= _utc(value["request_completed_at"]),
             "EXPORT_LINEAGE_INVALID")
    _require(type(value["status"]) is str and value["status"] in {"SUCCESS", "FAILED", "UNAVAILABLE"},
             "EXPORT_LINEAGE_INVALID")
    if value["status"] == "SUCCESS":
        attempt = value["attempt_id"]
        _require(type(attempt) is str and ATTEMPT_ID.fullmatch(attempt) is not None
                 and value["publication_path"] == PUBLICATION_HOST_ROOT + "/" + attempt
                 and value["error_stage"] is None, "EXPORT_LINEAGE_INVALID")
        for key in ("artifact_sha256", "metadata_sha256", "publication_sha256"):
            _sha(value[key])
    else:
        _require(all(value[k] is None for k in ("attempt_id", "artifact_sha256", "metadata_sha256",
                                               "publication_sha256", "publication_path"))
                 and type(value["error_stage"]) is str and value["error_stage"] in ERROR_STAGES,
                 "EXPORT_LINEAGE_INVALID")
    if record is not None:
        _require(record["clank_id"] == CLANK_ID and record["instance_id"] == INSTANCE_ID
                 and record["lane_id"] == LANE_ID and record["refresh_outcome"] == value["status"],
                 "EXPORT_LINEAGE_RECORD_BINDING_INVALID")
        if value["status"] == "SUCCESS":
            _require(_sha(record["snapshot_sha256"]) == _sha(value["artifact_sha256"]),
                     "EXPORT_LINEAGE_RECORD_BINDING_INVALID")
    return dict(value)


def translate_publication(accepted_root: Path, receipt: dict, *, request_id: str,
                          request_started_at: str, observed_at: str, adapter_package_sha: str,
                          adapter_artifact_sha256: str, adapter_package_version: str,
                          prior_attempt_id: str | None = None,
                          last_good_snapshot_ref: str | None = None) -> dict:
    """Verify accepted-only transport and emit the distinct ADR manifest row."""
    start, end, observed = _receipt(receipt, request_id=request_id, request_started_at=request_started_at,
                                    observed_at=observed_at, last_good_snapshot_ref=last_good_snapshot_ref)
    _require(receipt["status"] == "SUCCESS" and receipt["error_code"] is None
             and receipt["error_stage"] is None, "SUCCESS_RECEIPT_REQUIRED")
    attempt_id = receipt["attempt_id"]
    _require(type(attempt_id) is str and ATTEMPT_ID.fullmatch(attempt_id) is not None,
             "ATTEMPT_ID_INVALID")
    _require(prior_attempt_id is None or (type(prior_attempt_id) is str
             and ATTEMPT_ID.fullmatch(prior_attempt_id) is not None), "PRIOR_ATTEMPT_ID_INVALID")
    _require(attempt_id != prior_attempt_id, "PRIOR_EXPORT_CANNOT_BE_NEW_SUCCESS")
    root = Path(accepted_root)
    _require(root == ACCEPTED_ROOT, "ACCEPTED_ROOT_BINDING_INVALID")
    attempt = root / attempt_id
    _require(receipt["publication_path"] == PUBLICATION_HOST_ROOT + "/" + attempt_id,
             "HOST_PUBLICATION_BINDING_INVALID")
    _sealed_boundary(root, attempt)
    _kernel_readonly(root)
    m, metadata_hash = _read_json(attempt / META_NAME)
    seal, seal_hash = _read_json(attempt / SEAL_NAME)
    _metadata(m)
    _require(set(seal) == SEAL_KEYS, "PUBLICATION_SEAL_CONTRACT_INVALID")
    for key, value in {"status": "SUCCESS", "publisher_version": PUBLISHER_VERSION,
                       "export_format_version": EXPORT_FORMAT_VERSION, "attempt_id": attempt_id,
                       "metadata_sha256": metadata_hash, "artifact_sha256": m["sha256"],
                       "exporter_revision": EXPORTER_REVISION, "deployed_revision": DEPLOYED_REVISION}.items():
        _exact(seal, key, value, "PUBLICATION_SEAL_BINDING_INVALID")
    for key, expected in {"artifact_sha256": m["sha256"], "metadata_sha256": metadata_hash,
                          "publication_sha256": seal_hash, "snapshot_bytes": m["size_bytes"]}.items():
        _exact(receipt, key, expected, "REQUEST_ARTIFACT_BINDING_INVALID")
    created, published = _utc(m["export_completed_at"]), _utc(seal["published_at"])
    _require(start <= created <= published <= end, "PUBLICATION_OUTSIDE_CURRENT_REQUEST")
    row = _base(adapter_package_sha=adapter_package_sha, adapter_artifact_sha256=adapter_artifact_sha256,
                adapter_package_version=adapter_package_version, observed_at=observed_at,
                last_good_snapshot_ref=last_good_snapshot_ref)
    row.update(snapshot_created_at=m["export_completed_at"], child_as_of=m["child_as_of"],
               child_as_of_clock=m["child_as_of_clock"] if m["child_as_of"] is not None else None,
               schema_version=m["schema_version"], snapshot_path=(attempt / DB_NAME).as_posix(),
               snapshot_sha256=m["sha256"], snapshot_bytes=m["size_bytes"],
               integrity_result={"sqlite_integrity": "ok", "foreign_key_violations": 0},
               source_total_changes=m["source_access"]["total_changes"], refresh_outcome="SUCCESS",
               freshness_state="FRESH" if (observed-created).total_seconds() <= HORIZON["max_age_seconds"] else "STALE")
    if m["child_as_of"] is None:
        row["unavailable_reasons"].update({key: m["child_as_of_unavailable_reason"]
                                          for key in ("child_as_of", "child_as_of_clock")})
    else:
        row["child_execution_freshness"] = ("FRESH" if (observed-_utc(m["child_as_of"])).total_seconds()
                                             <= HORIZON["max_age_seconds"] else "STALE")
    row["child_export"] = _provenance(receipt)
    manifest_v1._validate_record(row, 0)
    try:
        # Reuse common copy verification only: no child-domain SQL here.
        manifest_v1.verify_copy(row, attempt / DB_NAME)
    except manifest_v1.SnapshotManifestError:
        raise FeaturePhoneExportError("SEALED_COPY_VERIFICATION_FAILED") from None
    _sealed_boundary(root, attempt)
    _require(_read_json(attempt / META_NAME)[1] == metadata_hash
             and _read_json(attempt / SEAL_NAME)[1] == seal_hash,
             "PUBLICATION_CHANGED_DURING_INTAKE")
    return row
