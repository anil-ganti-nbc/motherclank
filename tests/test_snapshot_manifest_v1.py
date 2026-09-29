"""ADR-0016: governed copy intake and per-lane provenance remain fail-closed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from motherclank import adapters, anomalies, cli, snapshot, synthesis
from motherclank.report import render_report, render_synthesis
from motherclank.snapshot_manifest import SnapshotManifestError, load_manifest

ADAPTER_SHA = "a" * 40
ARTIFACT_SHA = "b" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class HealthyAdapter:
    calls = 0

    def __init__(self, db_path: Path, run_at: str):
        self.db_path = db_path
        self.run_at = run_at

    def identity(self):
        self.calls += 1
        return type("Descriptor", (), {
            "clank_version": "1.0", "contract_version": "0.1.0-v3"})()

    def capabilities(self):
        return type("Capabilities", (), {
            "supports_delivery_accounting": False,
            "supports_telemetry": True,
            "supports_health": True,
        })()

    def status(self):
        return {"operational_state": "healthy"}

    def health(self):
        return {"sources": [{"source_id": "healthy-source", "status": "ok"}]}

    def last_run(self):
        return {"supported": True, "finished_at": self.run_at, "status": "ok"}

    def capability_states(self):
        return {"collection": {"state": "active", "evidence": "fixture run"}}


def _case(tmp_path: Path):
    now = datetime.now(UTC).replace(microsecond=0)
    source = tmp_path / "canonical.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'durable')")
    copy = tmp_path / "copy.db"
    with sqlite3.connect(source) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    as_of = _iso(now - timedelta(minutes=5))
    row = {
        "snapshot_contract_version": "1.0", "clank_id": "example-clank",
        "instance_id": "nas-canonical", "lane_id": "production",
        "source_host": "NAS", "source_path": str(source),
        "snapshot_created_at": _iso(now), "child_as_of": as_of,
        "child_as_of_clock": "native_run_row", "schema_version": "schema-v1",
        "child_source_revision": "c" * 40,
        "child_deployed_revision": "d" * 40,
        "snapshot_path": str(copy), "snapshot_sha256": digest,
        "snapshot_bytes": copy.stat().st_size,
        "integrity_result": {"sqlite_integrity": "ok", "foreign_key_violations": 0},
        "adapter_package_sha": ADAPTER_SHA,
        "adapter_artifact_sha256": ARTIFACT_SHA,
        "adapter_package_version": "0.1.0", "observer_contract_version": "0.2",
        "refresh_outcome": "SUCCESS", "freshness_state": "FRESH",
        "child_execution_freshness": "FRESH", "observed_at": _iso(now),
        "freshness_horizon": {
            "max_age_seconds": 3600, "clock": "native_run_row", "source": "latest run"},
        "error_code": None, "last_good_snapshot_ref": None,
    }
    manifest = tmp_path / "manifest.json"
    inventory = tmp_path / "inventory.yaml"
    inventory.write_text("deployments: []\n", encoding="utf-8")
    adapter = HealthyAdapter(copy, as_of)
    built = {"adapters": {"example-clank": adapter}, "versions": {},
             "qc_adapters": [],
             "expected_identities": {"example-clank": {
                 "instance_id": "nas-canonical", "lane_id": "production"}}}

    def save():
        manifest.write_text(json.dumps({
            "snapshot_contract_version": "1.0", "observed_at": _iso(now),
            "lanes": [row]}, sort_keys=True), encoding="utf-8")

    save()
    return row, manifest, inventory, built, adapter, save


def _build(tmp_path, manifest, inventory, built):
    return snapshot.build_snapshot(
        inventory_path=inventory, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path,
        snapshot_manifest_path=manifest,
        expected_adapter_package_sha=ADAPTER_SHA,
        expected_adapter_artifact_sha256=ARTIFACT_SHA)


def test_fresh_copy_is_verified_and_propagates_lineage(tmp_path):
    row, manifest, inventory, built, adapter, _ = _case(tmp_path)
    source_hash = hashlib.sha256(Path(row["source_path"]).read_bytes()).hexdigest()
    payload, warnings = _build(tmp_path, manifest, inventory, built)
    assert not warnings
    assert adapter.calls > 0
    assert payload["snapshot_contract_version"] == "1.0"
    block = payload["clanks"]["example-clank"]
    assert block["snapshot_provenance"]["snapshot_sha256"] == row["snapshot_sha256"]
    claim = synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]
    assert claim["state"] == "HEALTHY"
    assert claim["provenance"]["adapter_package_sha"] == ADAPTER_SHA
    assert claim["provenance"]["adapter_artifact_sha256"] == ARTIFACT_SHA
    assert row["snapshot_sha256"] in render_report(payload)
    assert row["snapshot_sha256"] in render_synthesis(synthesis.synthesize_fleet(payload))
    assert hashlib.sha256(Path(row["source_path"]).read_bytes()).hexdigest() == source_hash


@pytest.mark.parametrize("outcome,freshness", [
    ("FAILED", "REFRESH_FAILED"), ("UNAVAILABLE", "UNAVAILABLE")])
def test_failed_or_unavailable_refresh_never_probes_last_good(
        tmp_path, outcome, freshness):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    row.update(refresh_outcome=outcome, freshness_state=freshness,
               snapshot_created_at=None, snapshot_path=None,
               snapshot_sha256=None, snapshot_bytes=None,
               integrity_result={"sqlite_integrity": "UNKNOWN",
                                 "foreign_key_violations": None},
               error_code="SOURCE_UNAVAILABLE", last_good_snapshot_ref="e" * 64,
               child_as_of=None, child_as_of_clock=None,
               child_execution_freshness="UNKNOWN")
    row["unavailable_reasons"] = {
        "snapshot_created_at": "no copy produced",
        "child_as_of": "no current copy", "child_as_of_clock": "no current copy"}
    save()
    payload, _ = _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0
    block = payload["clanks"]["example-clank"]
    assert block["snapshot_provenance"]["last_good_snapshot_ref"] == "e" * 64
    assert block["observation"].startswith("SNAPSHOT_")
    claim = synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]
    assert claim["state"] == "UNKNOWN"
    assert "R0_SNAPSHOT" in claim["rules_applied"]


def test_recent_copy_of_stale_child_is_unknown_not_healthy(tmp_path):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    row["child_as_of"] = _iso(datetime.now(UTC) - timedelta(hours=2))
    row["child_execution_freshness"] = "STALE"
    save()
    payload, _ = _build(tmp_path, manifest, inventory, built)
    assert adapter.calls > 0  # current copy can still show source detail
    block = payload["clanks"]["example-clank"]
    assert block["observation"] == "CHILD_EXECUTION_STALE"
    assert synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]["state"] == "UNKNOWN"
    assert "| example-clank | UNKNOWN |" in render_report(payload)


def test_stale_child_cannot_claim_fresh_execution(tmp_path):
    row, manifest, inventory, built, _, save = _case(tmp_path)
    row["child_as_of"] = _iso(datetime.now(UTC) - timedelta(hours=2))
    save()
    with pytest.raises(SnapshotManifestError, match="stale/future child"):
        _build(tmp_path, manifest, inventory, built)


def test_copy_hash_mismatch_isolated_before_adapter_probe(tmp_path):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    row["snapshot_sha256"] = "0" * 64
    save()
    payload, warnings = _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0
    assert payload["clanks"]["example-clank"]["observation"] == "SNAPSHOT_REJECTED"
    assert synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]["state"] == "UNKNOWN"
    assert warnings


def test_declared_fk_zero_cannot_hide_actual_copy_violation(tmp_path):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    copy = Path(row["snapshot_path"])
    with sqlite3.connect(copy) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        db.execute("INSERT INTO child VALUES (999)")
    row["snapshot_bytes"] = copy.stat().st_size
    row["snapshot_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
    save()
    payload, warnings = _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0
    assert payload["clanks"]["example-clank"]["observation"] == "SNAPSHOT_REJECTED"
    assert any("integrity/FK" in message for message in warnings)


def test_unattempted_registry_lane_remains_unknown(tmp_path):
    _, manifest, inventory, built, adapter, _ = _case(tmp_path)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["lanes"] = []
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    payload, _ = _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0
    block = payload["clanks"]["example-clank"]
    assert block["error_code"] == "NO_MANIFEST_RECORD"
    assert synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]["state"] == "UNKNOWN"


def test_incompatible_major_and_missing_field_rejected(tmp_path):
    row, manifest, _, _, _, save = _case(tmp_path)
    row["snapshot_contract_version"] = "2.0"
    save()
    with pytest.raises(SnapshotManifestError, match="incompatible"):
        load_manifest(manifest)
    row["snapshot_contract_version"] = "1.0"
    del row["adapter_package_sha"]
    save()
    with pytest.raises(SnapshotManifestError, match="missing fields"):
        load_manifest(manifest)


def test_runtime_adapter_identity_must_be_supplied_and_match(tmp_path):
    row, manifest, inventory, built, _, save = _case(tmp_path)
    with pytest.raises(SnapshotManifestError, match="expected adapter package"):
        snapshot.build_snapshot(inventory_path=inventory, adapters_result=built,
                                real_state_dir=tmp_path, out_dir=tmp_path,
                                snapshot_manifest_path=manifest)
    row["adapter_package_sha"] = "f" * 40
    save()
    with pytest.raises(SnapshotManifestError, match="does not match runtime"):
        _build(tmp_path, manifest, inventory, built)
    row["adapter_package_sha"] = ADAPTER_SHA
    row["adapter_artifact_sha256"] = "f" * 64
    save()
    with pytest.raises(SnapshotManifestError, match="artifact digest"):
        _build(tmp_path, manifest, inventory, built)


def test_cli_requires_attested_identity_and_forwards_adapter_source(
        tmp_path, monkeypatch, capsys):
    _, manifest, inventory, built, _, _ = _case(tmp_path)
    calls = []

    def fake_build(real_state, **kwargs):
        calls.append((real_state, kwargs))
        return built

    monkeypatch.setattr(cli, "build_adapters", fake_build)
    base = ["harvest", "--inventory", str(inventory), "--real-state",
            str(tmp_path), "--snapshot-manifest", str(manifest), "--out",
            str(tmp_path), "--adapters-src", str(tmp_path / "adapter-source"),
            "--dry-run"]
    assert cli.main(base) == 6
    assert "snapshot manifest rejected" in capsys.readouterr().err
    assert cli.main(base + ["--expected-adapter-package-sha", ADAPTER_SHA,
                            "--expected-adapter-artifact-sha256",
                            "sha256:" + ARTIFACT_SHA]) == 0
    assert calls[-1][1]["diagnostic_clank_path"] == tmp_path / "adapter-source"


def test_duplicate_active_clank_lane_rejected(tmp_path):
    _, manifest, _, _, _, _ = _case(tmp_path)
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    second = dict(doc["lanes"][0], lane_id="secondary")
    doc["lanes"].append(second)
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SnapshotManifestError, match="multiple active lanes"):
        load_manifest(manifest)


@pytest.mark.parametrize("field,wrong", [
    ("instance_id", "nas-soak"), ("lane_id", "experimental")])
def test_valid_copy_from_wrong_instance_or_lane_cannot_be_probed(
        tmp_path, field, wrong):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    row[field] = wrong
    save()
    with pytest.raises(SnapshotManifestError, match=f"manifest {field}"):
        _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0


def test_manifest_requires_independent_registry_identity(tmp_path):
    _, manifest, inventory, built, adapter, _ = _case(tmp_path)
    built.pop("expected_identities")
    with pytest.raises(SnapshotManifestError, match="no expected instance_id/lane_id"):
        _build(tmp_path, manifest, inventory, built)
    assert adapter.calls == 0


def test_registry_retains_explicit_snapshot_identity_and_rejects_partial_pair(tmp_path):
    registry = tmp_path / "registry.json"
    base = {"extend_builtin": False, "example-clank": {
        "module": "fixture.adapters", "class": "FixtureAdapter", "db": "copy.db",
        "instance_id": "nas-canonical", "lane_id": "production"}}
    registry.write_text(json.dumps(base), encoding="utf-8")
    assert adapters.load_registry(registry)["example-clank"]["instance_id"] == \
        "nas-canonical"
    del base["example-clank"]["lane_id"]
    registry.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="lane_id"):
        adapters.load_registry(registry)


def test_invalid_capability_vocabulary_cannot_yield_healthy(tmp_path):
    row, manifest, inventory, built, adapter, _ = _case(tmp_path)
    adapter.capability_states = lambda: {
        "collection": {"state": "invented_healthy", "evidence": "not canonical"}}
    payload, _ = _build(tmp_path, manifest, inventory, built)
    block = payload["clanks"]["example-clank"]
    assert block["capability_states"]["observation"] == "FAILED_ADAPTER"
    assert synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]["state"] == "UNKNOWN"


def test_anomaly_cites_lineage_and_unavailable_is_not_recovery():
    provenance = {"snapshot_sha256": "s" * 64,
                  "adapter_package_sha": ADAPTER_SHA,
                  "adapter_artifact_sha256": ARTIFACT_SHA,
                  "adapter_package_version": "0.1.0",
                  "observer_contract_version": "0.2",
                  "refresh_outcome": "SUCCESS", "freshness_state": "FRESH",
                  "child_execution_freshness": "FRESH", "observed_at": "2026-09-29T10:00:00Z",
                  "instance_id": "nas", "lane_id": "production"}
    first = {"harvested_at_utc": "2026-09-29T10:00:00Z", "content_hash": "h1",
             "snapshot_manifest_sha256": "mh1",
             "clanks": {"example-clank": {
                 "health": {"sources": [{"source_id": "x", "status": "failed"}]},
                 "snapshot_provenance": provenance}}}
    second = {"harvested_at_utc": "2026-09-29T11:00:00Z", "content_hash": "h2",
              "clanks": {"example-clank": {
                  "observation": "SNAPSHOT_UNAVAILABLE",
                  "snapshot_provenance": {**provenance,
                                          "refresh_outcome": "UNAVAILABLE",
                                          "freshness_state": "UNAVAILABLE"}}}}
    found = anomalies.detect([first, second])
    issue = next(x for x in found if x["type"] == "SOURCE_DEGRADED_AT_FIRST_OBSERVATION")
    assert issue["lifecycle"] != "RECOVERED"
    assert issue["provenance"]["source_snapshot"]["snapshot_sha256"] == "s" * 64
    assert issue["provenance"]["source_snapshot"]["adapter_package_sha"] == ADAPTER_SHA


def test_producer_manifest_flows_to_consumer_after_bind_mount_mapping(tmp_path):
    """Exercise actual producer output; translate only /app mount in this host test."""
    producer_path = Path(__file__).resolve().parents[1] / "scripts" / "nas_snapshot_v1.py"
    spec = importlib.util.spec_from_file_location("nas_snapshot_v1_integration", producer_path)
    assert spec and spec.loader
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)

    source = tmp_path / "canonical" / "live.db"
    source.parent.mkdir()
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, finished_at TEXT)")
        writer.execute("INSERT INTO runs(finished_at) VALUES (?)", (_iso(datetime.now(UTC)),))
        writer.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO metadata VALUES ('schema', 'schema-v1')")
        writer.commit()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        spec_doc = {
            "snapshot_contract_version": "1.0",
            "allowed_source_roots": [str(source.parent)],
            "lanes": [{
                "clank_id": "example-clank", "instance_id": "nas-canonical",
                "lane_id": "production", "source_host": "NAS",
                "source_path": str(source), "snapshot_filename": "copy.db",
                "child_as_of": {"query": "SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1",
                                "clock": "native_run_row"},
                "schema_version": {"query": "SELECT value FROM metadata WHERE name='schema'"},
                "child_source_revision": None,
                "child_deployed_revision": "d" * 40,
                "child_deployed_revision_evidence": "pinned image label",
                "unavailable_reasons": {
                    "child_source_revision": "SOURCE_REVISION_NOT_EXPOSED"},
                "freshness_horizon": {"max_age_seconds": 3600,
                                      "clock": "native_run_row",
                                      "source": "test lane policy"},
                "adapter_package_sha": ADAPTER_SHA,
                "adapter_artifact_sha256": ARTIFACT_SHA,
                "adapter_package_version": "0.1.0",
                "observer_contract_version": "0.2",
            }],
        }
        output = tmp_path / "snapshots"
        output.mkdir()
        manifest_path, generated = producer.produce(spec_doc, output)
        row = generated["lanes"][0]
        assert row["snapshot_path"] == "/app/real-state/copy.db"
        assert row["refresh_outcome"] == "SUCCESS"
        # A real container binds manifest_path.parent at /app/real-state.
        # Map that mount to its host path for this Windows/Linux unit test.
        row["snapshot_path"] = row["snapshot_host_path"]
        manifest_path.write_text(json.dumps(generated), encoding="utf-8")
        adapter = HealthyAdapter(Path(row["snapshot_host_path"]), row["child_as_of"])
        inventory = tmp_path / "inventory.yaml"
        inventory.write_text("deployments: []\n", encoding="utf-8")
        built = {"adapters": {"example-clank": adapter}, "versions": {},
                 "qc_adapters": [],
                 "expected_identities": {"example-clank": {
                     "instance_id": "nas-canonical", "lane_id": "production"}}}
        payload, warnings = _build(tmp_path, manifest_path, inventory, built)
        assert not warnings
        assert synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]["state"] == "HEALTHY"
        assert payload["clanks"]["example-clank"]["snapshot_provenance"][
            "snapshot_sha256"] == row["snapshot_sha256"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    finally:
        writer.close()
