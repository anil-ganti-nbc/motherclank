"""Sealed transport is separate from child domain/refresh authority."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from motherclank import feature_phone_export as fp, snapshot_manifest, snapshot, synthesis

NOW = datetime(2026, 9, 30, 13, 20, tzinfo=UTC)
START = "2026-09-30T13:19:20Z"
OBSERVED = "2026-09-30T13:19:33Z"
ATTEMPT = "export-20260930T131930Z-0123456789abcdef"
PRIOR = "export-20260930T131900Z-fedcba9876543210"
ARGS = dict(request_id="fp-request-20260930-0123456789abcdef", request_started_at=START,
            observed_at=OBSERVED, adapter_package_sha="a"*40,
            adapter_artifact_sha256="b"*64, adapter_package_version="0.1.0")


@pytest.fixture(autouse=True)
def portable_declared_source(tmp_path, monkeypatch):
    # Native Windows Path.is_absolute intentionally rejects a POSIX-only
    # manifest source string. Substitute only this metadata identity in local
    # fixtures; no file exists or is mounted/opened at the declared source.
    monkeypatch.setattr(fp, "CANONICAL_SOURCE_PATH", str(tmp_path/"canonical-not-mounted.db"))


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt():
    return dict(request_format_version="1.0", status="SUCCESS", request_id=ARGS["request_id"],
                request_started_at=START, request_completed_at="2026-09-30T13:19:32Z",
                attempt_id=ATTEMPT, exporter_revision=fp.EXPORTER_REVISION, export_image=fp.EXPORT_IMAGE,
                deployed_revision=fp.DEPLOYED_REVISION, artifact_sha256=None, metadata_sha256=None,
                publication_sha256=None, snapshot_bytes=None,
                publication_path=fp.PUBLICATION_HOST_ROOT+"/"+ATTEMPT, error_code=None,
                error_stage=None, last_good_snapshot_ref=None)


def _metadata(database):
    return dict(status="VERIFIED_PRIVATE", export_format_version="1.0", publisher_version="1.0",
                clank_id=fp.CLANK_ID, instance_id=fp.INSTANCE_ID, lane_id=fp.LANE_ID,
                canonical_source_path=fp.CANONICAL_SOURCE_PATH, deployed_revision=fp.DEPLOYED_REVISION,
                exporter_revision=fp.EXPORTER_REVISION, source_revision=None,
                source_revision_unavailable_reason="NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE",
                schema_version=7, child_as_of="2026-09-30T13:19:10Z", child_as_of_clock="NATIVE_RUN_ROW_UTC",
                child_as_of_unavailable_reason=None, export_completed_at="2026-09-30T13:19:30Z",
                artifact_filename=fp.DB_NAME, artifact_path=fp.DB_NAME, size_bytes=database.stat().st_size,
                sha256=_digest(database), integrity_check="ok", foreign_key_violations=0,
                sqlite_runtime_version=sqlite3.sqlite_version,
                source_access=dict(mode="ro", query_only=True, authorizer="READ_ALLOWLIST_V1",
                                   total_changes=0, copy_total_changes=0, schema_cookie_unchanged=True,
                                   sidecars_before={k:False for k in ("-wal", "-shm", "-journal")},
                                   sidecars_after={"-wal":True, "-shm":True, "-journal":False},
                                   sidecar_authority="CHILD_SQLITE_COORDINATION_NOT_DOMAIN_WRITES"))


def _seal(meta, metadata_hash):
    return dict(status="SUCCESS", publisher_version="1.0", export_format_version="1.0", attempt_id=ATTEMPT,
                metadata_sha256=metadata_hash, artifact_sha256=meta["sha256"],
                exporter_revision=fp.EXPORTER_REVISION, deployed_revision=fp.DEPLOYED_REVISION,
                published_at="2026-09-30T13:19:31Z")


@pytest.fixture
def publication(tmp_path, monkeypatch):
    root = tmp_path/"accepted"
    attempt = root/ATTEMPT
    attempt.mkdir(parents=True)
    database = attempt/fp.DB_NAME
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE harmless_fixture (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO harmless_fixture VALUES (1)")
    metadata = _metadata(database)
    receipt = _receipt()
    def refresh(meta=metadata, seal_change=None):
        (attempt/fp.META_NAME).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        seal = _seal(meta, _digest(attempt/fp.META_NAME))
        if seal_change: seal.update(seal_change)
        (attempt/fp.SEAL_NAME).write_text(json.dumps(seal, sort_keys=True), encoding="utf-8")
        receipt.update(artifact_sha256=meta["sha256"], metadata_sha256=_digest(attempt/fp.META_NAME),
                       publication_sha256=_digest(attempt/fp.SEAL_NAME), snapshot_bytes=meta["size_bytes"])
    refresh()
    # Windows fixtures exercise real bytes/hash/SQLite verification and strict
    # synthetic POSIX stat checks. There is no caller-accessible production bypass.
    def fixture_boundary(bound_root, bound_attempt):
        assert bound_root == root and bound_attempt == attempt
        assert {p.name for p in attempt.iterdir()} == fp.FILES
        for path in (root, attempt, *(attempt/n for n in fp.FILES)):
            directory = path.is_dir()
            info = SimpleNamespace(st_mode=(stat.S_IFDIR|0o550 if directory else stat.S_IFREG|0o440),
                                   st_uid=0, st_gid=10001, st_nlink=1)
            fp._check_stat(info, directory=directory)
    monkeypatch.setattr(fp, "ACCEPTED_ROOT", root)
    monkeypatch.setattr(fp, "_sealed_boundary", fixture_boundary)
    monkeypatch.setattr(fp, "_kernel_readonly", lambda p: None)
    monkeypatch.setattr(fp, "utc_now", lambda: NOW)
    return root, attempt, metadata, receipt, refresh


def test_exact_sealed_transport_mapping_and_optional_lineage(publication):
    root, attempt, meta, receipt, _ = publication
    before = {n:_digest(attempt/n) for n in fp.FILES}
    row = fp.translate_publication(root, receipt, prior_attempt_id=PRIOR, **ARGS)
    assert row["instance_id"] == fp.INSTANCE_ID
    assert row["source_path"] == fp.CANONICAL_SOURCE_PATH
    assert row["snapshot_path"] == (attempt/fp.DB_NAME).as_posix()
    assert row["snapshot_created_at"] == meta["export_completed_at"]
    assert row["child_as_of"] == meta["child_as_of"] != row["snapshot_created_at"]
    assert row["child_source_revision"] is None
    assert row["child_deployed_revision"] == fp.DEPLOYED_REVISION
    assert row["snapshot_contract_version"] == row["child_export"]["export_format_version"] == "1.0"
    assert row["freshness_horizon"]["max_age_seconds"] == 36000
    assert row["freshness_state"] == row["child_execution_freshness"] == "FRESH"
    assert row["source_total_changes"] == 0
    assert snapshot_manifest.lineage(row)["child_export"]["metadata_sha256"] == receipt["metadata_sha256"]
    assert {n:_digest(attempt/n) for n in fp.FILES} == before


@pytest.mark.parametrize("field,value", [
    ("instance_id", "feature-phone-nas-experimental"), ("schema_version", "7"),
    ("source_revision", fp.EXPORTER_REVISION), ("deployed_revision", "a"*40),
    ("exporter_revision", "b"*40), ("canonical_source_path", "/secrets/webhook"),
    ("foreign_key_violations", False), ("artifact_path", "../private.db"),
    ("extra_secret", "DO_NOT_LOG_PAYLOAD"), ("child_as_of", "2026-09-30T13:20:20Z"),
    ("child_as_of", "2026-09-30T18:49:10+05:30"),
])
def test_metadata_strict_types_identity_no_secret_echo(publication, field, value):
    root, _, meta, receipt, refresh = publication
    meta[field] = value
    refresh()
    with pytest.raises(fp.FeaturePhoneExportError) as exc:
        fp.translate_publication(root, receipt, **ARGS)
    assert "DO_NOT_LOG" not in str(exc.value) and "/secrets" not in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("request_id", "other-request"), ("request_started_at", "2026-09-30T13:19:19Z"),
    ("request_completed_at", "2026-09-30T13:19:29Z"),
    ("request_completed_at", "2026-09-30T13:21:29Z"),
    ("export_image", "sha256:"+"c"*64), ("artifact_sha256", "d"*64),
    ("metadata_sha256", "e"*64), ("publication_sha256", "f"*64),
    ("snapshot_bytes", True), ("publication_path", "/private/staging"),
    ("status", ["SUCCESS"]), ("extra_secret", "DO_NOT_LOG_WEBHOOK"),
])
def test_receipt_independent_request_hash_pins_and_times(publication, field, value):
    root, _, _, receipt, _ = publication
    receipt[field] = value
    with pytest.raises(fp.FeaturePhoneExportError) as exc:
        fp.translate_publication(root, receipt, **ARGS)
    assert "DO_NOT_LOG" not in str(exc.value)


def test_previous_export_cannot_be_new_success_even_if_native_clock_current(publication):
    root, _, _, receipt, _ = publication
    with pytest.raises(fp.FeaturePhoneExportError, match="PRIOR_EXPORT_CANNOT_BE_NEW_SUCCESS"):
        fp.translate_publication(root, receipt, prior_attempt_id=ATTEMPT, **ARGS)


def test_publication_from_before_request_cannot_be_success(publication):
    root, _, meta, receipt, refresh = publication
    meta["export_completed_at"] = "2026-09-30T13:19:15Z"
    refresh()
    with pytest.raises(fp.FeaturePhoneExportError, match="OUTSIDE_CURRENT_REQUEST"):
        fp.translate_publication(root, receipt, **ARGS)


def test_unavailable_clock_sentinel_translates_reasoned_null(publication):
    root, _, meta, receipt, refresh = publication
    meta.update(child_as_of=None, child_as_of_clock="UNAVAILABLE", child_as_of_unavailable_reason="NO_NATIVE_RUN_CLOCK")
    refresh()
    row = fp.translate_publication(root, receipt, **ARGS)
    assert row["child_as_of"] is row["child_as_of_clock"] is None
    assert row["child_execution_freshness"] == "UNKNOWN"
    assert row["unavailable_reasons"]["child_as_of_clock"] == "NO_NATIVE_RUN_CLOCK"


def test_new_export_does_not_freshen_stale_child(publication):
    root, _, meta, receipt, refresh = publication
    meta["child_as_of"] = "2026-09-29T23:19:10Z"
    refresh()
    row = fp.translate_publication(root, receipt, **ARGS)
    assert row["freshness_state"] == "FRESH" and row["child_execution_freshness"] == "STALE"


@pytest.mark.parametrize("mutation", ["database", "sidecar", "seal", "duplicate"])
def test_transport_tamper_and_sidecar_rejection(publication, mutation):
    root, attempt, _, receipt, refresh = publication
    if mutation == "database":
        with (attempt/fp.DB_NAME).open("ab") as stream: stream.write(b"tampered")
    elif mutation == "sidecar":
        Path(str(attempt/fp.DB_NAME)+"-wal").write_bytes(b"unhashed")
        # Boundary fixture lets common copy verifier independently reject it.
        fp._sealed_boundary = lambda r, a: None
    elif mutation == "seal":
        refresh(seal_change={"status":"VERIFIED_PRIVATE"})
    else:
        path = attempt/fp.META_NAME
        path.write_text('{"status":"VERIFIED_PRIVATE",'+path.read_text()[1:], encoding="utf-8")
    with pytest.raises(fp.FeaturePhoneExportError):
        fp.translate_publication(root, receipt, **ARGS)


@pytest.mark.parametrize("mode,uid,gid,nlink,isdir", [
    (stat.S_IFREG|0o440, 10001, 10001, 1, False),
    (stat.S_IFREG|0o640, 0, 10001, 1, False),
    (stat.S_IFREG|0o440, 0, 10001, 2, False),
    (stat.S_IFLNK|0o440, 0, 10001, 1, False),
    (stat.S_IFDIR|0o550, 0, 0, 1, True),
])
def test_root_sealed_permission_type_checks(mode, uid, gid, nlink, isdir):
    with pytest.raises(fp.FeaturePhoneExportError, match="NOT_ROOT_SEALED"):
        fp._check_stat(SimpleNamespace(st_mode=mode,st_uid=uid,st_gid=gid,st_nlink=nlink), directory=isdir)


def _failure(status="FAILED", ref=None):
    result = _receipt()
    result.update(status=status, attempt_id=None, publication_path=None,
                  error_code="EXPORT_TIMEOUT", error_stage="EXPORT", last_good_snapshot_ref=ref)
    return result


@pytest.mark.parametrize("status", ["FAILED", "UNAVAILABLE"])
def test_failure_retains_history_without_opening_old_artifact(monkeypatch, status):
    monkeypatch.setattr(fp, "utc_now", lambda: NOW)
    def never(*args, **kwargs): raise AssertionError("old artifact must not be read")
    monkeypatch.setattr(Path, "open", never)
    monkeypatch.setattr(Path, "read_bytes", never)
    ref = "d"*64
    row = fp.translate_failure(_failure(status, ref), last_good_snapshot_ref=ref, **ARGS)
    assert row["refresh_outcome"] == status
    assert row["freshness_state"] == ("REFRESH_FAILED" if status=="FAILED" else "UNAVAILABLE")
    assert row["last_good_snapshot_ref"] == ref
    assert row["snapshot_path"] is row["snapshot_sha256"] is row["snapshot_bytes"] is None
    assert row["child_execution_freshness"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("publication_path", "/old/export"), ("artifact_sha256", "a"*64),
                                        ("error_code", "secret https://example"), ("error_stage", "SELECT secret")])
def test_failure_envelope_cannot_smuggle_current_or_secret_data(monkeypatch, field, value):
    monkeypatch.setattr(fp, "utc_now", lambda: NOW)
    receipt = _failure(); receipt[field] = value
    with pytest.raises(fp.FeaturePhoneExportError) as exc:
        fp.translate_failure(receipt, **ARGS)
    assert "https" not in str(exc.value) and "SELECT" not in str(exc.value)


def test_kernel_ro_mount_parser_does_not_accept_rw_or_nested(monkeypatch):
    monkeypatch.setattr(fp, "os", SimpleNamespace(name="posix"))
    read = Path.read_text
    text = ["42 20 0:1 /accepted /app/feature-phone-accepted ro,relatime - ext4 /dev/test rw\n"]
    def mounted(path, *args, **kwargs):
        return text[0] if path.as_posix()=="/proc/self/mountinfo" else read(path,*args,**kwargs)
    monkeypatch.setattr(Path, "read_text", mounted)
    fp._kernel_readonly(Path("/app/feature-phone-accepted"))
    text[0] = text[0].replace("ro,relatime", "rw,relatime")
    with pytest.raises(fp.FeaturePhoneExportError, match="NOT_KERNEL_READONLY"):
        fp._kernel_readonly(Path("/app/feature-phone-accepted"))
    text[0] = "42 20 0:1 /accepted /app/feature-phone-accepted ro - ext4 /dev/test rw\n43 42 0:2 /state /app/feature-phone-accepted/private ro - ext4 /dev/test rw\n"
    with pytest.raises(fp.FeaturePhoneExportError, match="NESTED_ACCEPTED"):
        fp._kernel_readonly(Path("/app/feature-phone-accepted"))


def test_failed_feature_phone_fleet_harvest_is_unknown_other_four_continue(tmp_path, monkeypatch):
    from test_snapshot_manifest_v1 import _case, _build, HealthyAdapter
    template, manifest, inventory, built, _, _ = _case(tmp_path)
    monkeypatch.setattr(fp, "utc_now", lambda: NOW)
    monkeypatch.setattr(snapshot_manifest, "utc_now", lambda: NOW)
    built["adapters"].clear(); built["expected_identities"].clear(); built["expected_schema_versions"].clear()
    rows=[]
    for index in range(4):
        row=copy.deepcopy(template); cid="other-child-"+str(index)
        row.update(clank_id=cid, snapshot_created_at=OBSERVED, observed_at=OBSERVED,
                   child_as_of="2026-09-30T13:19:10Z")
        rows.append(row)
        built["adapters"][cid]=HealthyAdapter(Path(row["snapshot_path"]), row["child_as_of"])
        built["expected_identities"][cid] = dict(instance_id=row["instance_id"], lane_id=row["lane_id"])
        built["expected_schema_versions"][cid] = row["schema_version"]
    failure=fp.translate_failure(_failure(ref="d"*64), last_good_snapshot_ref="d"*64, **ARGS)
    rows.append(failure)
    fp_adapter=HealthyAdapter(tmp_path/"old_not_current.db", "2026-09-30T13:19:10Z")
    built["adapters"][fp.CLANK_ID]=fp_adapter
    built["expected_identities"][fp.CLANK_ID] = dict(instance_id=fp.INSTANCE_ID,lane_id=fp.LANE_ID)
    manifest.write_text(json.dumps(dict(snapshot_contract_version="1.0", observed_at=OBSERVED,lanes=rows)),encoding="utf-8")
    payload,warnings=_build(tmp_path,manifest,inventory,built)
    assert not warnings and fp_adapter.calls==0
    assert payload["clanks"][fp.CLANK_ID]["observation"]=="SNAPSHOT_REFRESH_FAILED"
    assert payload["clanks"][fp.CLANK_ID]["snapshot_provenance"]["last_good_snapshot_ref"]=="d"*64
    claims=synthesis.synthesize_fleet(payload)["clanks"]
    assert claims[fp.CLANK_ID]["state"]=="UNKNOWN"
    assert all(claims["other-child-"+str(i)]["state"]=="HEALTHY" for i in range(4))
    assert all(built["adapters"]["other-child-"+str(i)].calls>0 for i in range(4))
