"""Forensic diagnostics cannot expose payloads, retry failures or admit history."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from motherclank.snapshot_manifest import load_manifest, verify_copy, SnapshotManifestError
from test_nas_snapshot_v1 import _source, _spec, snapshot

ROOT = Path(__file__).resolve().parents[1]
gate_spec = importlib.util.spec_from_file_location("forensic_gate", ROOT / "scripts/nas_task14_manifest_gate.py")
gate = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(gate)


def _error():
    error = sqlite3.OperationalError("SECRET_PAYLOAD_DO_NOT_LOG")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"
    return error


def _produce(tmp_path, monkeypatch, failure=None, **overrides):
    source = tmp_path / "child/live.db"
    writer = _source(source)
    output = tmp_path / "snapshots"
    output.mkdir()
    connect = sqlite3.connect

    class SourceProxy:
        def __init__(self, con): self.con = con
        def __getattr__(self, key): return getattr(self.con, key)
        def backup(self, target):
            if failure == "backup": raise _error()
            return self.con.backup(getattr(target, "con", target))

    class CopyProxy:
        def __init__(self, con): self.con = con
        def __getattr__(self, key): return getattr(self.con, key)
        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check" and failure == "integrity":
                raise _error()
            if sql == "PRAGMA integrity_check" and failure == "integrity_result":
                class Bad:
                    def fetchone(self): return ("SECRET_CORRUPT_PAYLOAD",)
                return Bad()
            return self.con.execute(sql, *args)

    calls = []
    def injected(database, *args, **kwargs):
        calls.append(str(database))
        if str(database).startswith("file:"):
            if failure == "open": raise _error()
            return SourceProxy(connect(database, *args, **kwargs))
        con = connect(database, *args, **kwargs)
        return CopyProxy(con) if failure in ("integrity", "integrity_result") else con

    try:
        with monkeypatch.context() as patch:
            patch.setattr(snapshot.sqlite3, "connect", injected)
            path, doc = snapshot.produce(_spec(source, **overrides), output)
        diagnostics = json.loads((path.parent / "refresh-diagnostics.json").read_text())
        return source, path, doc["lanes"][0], diagnostics["lanes"][0], calls
    finally:
        writer.close()


@pytest.mark.parametrize("failure,stage,operation,destination,backup", [
    ("open", "SOURCE_OPEN", "SOURCE_CONNECT", False, False),
    ("backup", "SQLITE_BACKUP", "BACKUP", True, False),
    ("integrity", "INTEGRITY_CHECK", "INTEGRITY_QUERY", True, True),
])
def test_exact_sqlite_stage_and_safe_error_metadata(tmp_path, monkeypatch, failure, stage, operation, destination, backup):
    source, path, row, detail, calls = _produce(tmp_path, monkeypatch, failure)
    assert row["refresh_outcome"] == "FAILED"
    assert row["freshness_state"] == "REFRESH_FAILED"
    assert row["child_execution_freshness"] == "UNKNOWN"
    assert row["snapshot_path"] is None and row["snapshot_sha256"] is None
    assert detail["failure_stage"] == stage
    assert detail["failure_operation"] == operation
    assert detail["sqlite_error_class"] == "OperationalError"
    assert detail["sqlite_error_code"] == sqlite3.SQLITE_BUSY
    assert detail["sqlite_error_name"] == "SQLITE_BUSY"
    assert detail["destination_created"] == destination
    assert detail["backup_completed"] == backup
    assert not detail["integrity_completed"]
    assert detail["elapsed_seconds"] >= 0
    assert sum(call.startswith("file:") for call in calls) == 1  # No retry.
    assert "SECRET" not in (path.parent / "refresh-diagnostics.json").read_text()
    assert not list(path.parent.glob("*.db"))
    assert not list(path.parent.glob("*.partial"))
    assert load_manifest(path)["lanes"][0]["freshness_state"] == "REFRESH_FAILED"


def test_completed_integrity_failure_is_distinct_from_query_exception(tmp_path, monkeypatch):
    _, path, row, detail, _ = _produce(tmp_path, monkeypatch, "integrity_result")
    assert row["error_code"] == "SQLITE_INTEGRITY_FAILED"
    assert detail["integrity_completed"] is True
    assert detail["fk_completed"] is False
    assert detail["failure_stage"] == "INTEGRITY_CHECK"
    assert detail["sqlite_error_class"] is None
    assert "SECRET" not in path.read_text()


@pytest.mark.parametrize("outcome,freshness,reason", [
    ("FAILED", "REFRESH_FAILED", "REFRESH_FAILED"),
    ("UNAVAILABLE", "UNAVAILABLE", "REFRESH_UNAVAILABLE"),
])
def test_null_failed_paths_are_structural_evidence_not_binding_mismatch(tmp_path, monkeypatch, outcome, freshness, reason):
    _, path, row, _, _ = _produce(tmp_path, monkeypatch, "open")
    row.update(refresh_outcome=outcome, freshness_state=freshness)
    doc = json.loads(path.read_text()); doc["lanes"] = [row]
    path.write_text(json.dumps(doc))
    load_manifest(path)
    with pytest.raises(RuntimeError, match="^" + reason + ":"):
        gate.validate_attempt_binding(row, "nas-canonical", "default", row["source_path"], "oem_radar.db", path.parent / "oem_radar.db")
    # Run the same rejection in a real child process: failure must exit nonzero.
    code = "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('gate',sys.argv[1]); g=importlib.util.module_from_spec(s); s.loader.exec_module(g); r=json.load(open(sys.argv[2]))['lanes'][0]; g.validate_attempt_binding(r,'nas-canonical','default',r['source_path'],'oem_radar.db',g.Path(sys.argv[2]).parent/'oem_radar.db')"
    result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts/nas_task14_manifest_gate.py"), str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode == 1
    assert reason in result.stderr and "copy_binding_mismatch" not in result.stderr


def test_success_still_requires_exact_path_and_hash(tmp_path, monkeypatch):
    _, path, row, detail, _ = _produce(tmp_path, monkeypatch)
    copy = Path(row["snapshot_host_path"])
    gate.validate_attempt_binding(row, "nas-canonical", "default", row["source_path"], "oem_radar.db", copy)
    changed = dict(row, snapshot_path="/app/real-state/wrong.db")
    with pytest.raises(RuntimeError, match="copy_binding_mismatch"):
        gate.validate_attempt_binding(changed, "nas-canonical", "default", row["source_path"], "oem_radar.db", copy)
    mapped = dict(row, snapshot_path=str(copy))
    verify_copy(mapped, copy)
    with pytest.raises(SnapshotManifestError, match="SHA-256 mismatch"):
        verify_copy(dict(mapped, snapshot_sha256="0" * 64), copy)
    assert detail["failure_stage"] is None
    assert detail["backup_completed"] and detail["integrity_completed"] and detail["fk_completed"]


def test_retained_proof_is_history_not_configured_fallback(tmp_path, monkeypatch):
    ref = "sha256:" + "d" * 64
    _, path, row, detail, _ = _produce(tmp_path, monkeypatch, "backup", retained_prior_snapshot_refs=[ref])
    assert row["last_good_snapshot_ref"] is None
    assert row["unavailable_reasons"]["last_good_snapshot_ref"] == "NO_CONFIGURED_LAST_GOOD_REFERENCE"
    assert detail["retained_prior_snapshot_refs"] == [ref]
    assert detail["retained_prior_snapshot_evidence"] == "RETAINED_PRIOR_SNAPSHOT_NOT_ADMITTED_AS_FALLBACK"
    assert "NO_RETAINED_PRIOR_SNAPSHOT" not in json.dumps(detail)
    load_manifest(path)


def test_configured_last_good_does_not_freshen_failed_attempt(tmp_path, monkeypatch):
    ref = "sha256:" + "e" * 64
    _, path, row, detail, _ = _produce(tmp_path, monkeypatch, "backup", last_good_snapshot_ref=ref)
    assert row["last_good_snapshot_ref"] == detail["configured_last_good_reference"] == ref
    assert row["freshness_state"] == "REFRESH_FAILED"
    assert row["child_execution_freshness"] == "UNKNOWN"
    assert row["snapshot_path"] is None
    load_manifest(path)


def test_unknown_native_clock_stays_unknown(tmp_path, monkeypatch):
    _, path, row, _, _ = _produce(tmp_path, monkeypatch, child_as_of=None,
        unavailable_reasons={"child_source_revision": "NOT_EXPOSED", "child_as_of": "NATIVE_CLOCK_UNAVAILABLE"})
    assert row["refresh_outcome"] == "SUCCESS"
    assert row["freshness_state"] == "FRESH"
    assert row["child_execution_freshness"] == "UNKNOWN"
    doc = json.loads(path.read_text())
    doc["lanes"][0]["snapshot_path"] = row["snapshot_host_path"]
    path.write_text(json.dumps(doc))  # Translate only the container bind path.
    load_manifest(path)


def test_cli_real_exit_codes(tmp_path):
    source = tmp_path / "child/missing.db"; source.parent.mkdir()
    output = tmp_path / "snapshots"; output.mkdir()
    spec = tmp_path / "spec.json"; spec.write_text(json.dumps(_spec(source)))
    argv = [sys.executable, str(ROOT / "scripts/nas_snapshot_v1.py"), "--spec", str(spec), "--output-root", str(output)]
    attempt = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert attempt.returncode == 0  # Producer records failure; admission rejects it.
    manifest = load_manifest(Path(attempt.stdout.strip()))
    assert manifest["lanes"][0]["refresh_outcome"] == "UNAVAILABLE"
    bad = _spec(source); bad["snapshot_contract_version"] = "2.0"; spec.write_text(json.dumps(bad))
    rejected = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert rejected.returncode == 2 and rejected.stdout == ""
