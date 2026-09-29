"""ADR-0016 NAS snapshot producer: consistency, provenance, and fail-closed rules."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "nas_snapshot_v1.py"
MODULE_SPEC = importlib.util.spec_from_file_location("nas_snapshot_v1", MODULE_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
snapshot = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(snapshot)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(path: Path, *, finished_at: str | None = None, bad_fk: bool = False) -> sqlite3.Connection:
    path.parent.mkdir()
    writer = sqlite3.connect(str(path))
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("CREATE TABLE metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    writer.execute("INSERT INTO metadata VALUES ('schema', '7')")
    writer.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, finished_at TEXT)")
    writer.execute(
        "INSERT INTO runs(finished_at) VALUES (?)",
        (finished_at or datetime.now(timezone.utc).isoformat(),),
    )
    writer.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    writer.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
    if bad_fk:
        writer.execute("INSERT INTO child(parent_id) VALUES (999)")
    writer.commit()
    return writer


def _lane(path: Path, *, instance_id: str = "nas-canonical", max_age: int = 3600) -> dict:
    return {
        "clank_id": "oem-radar",
        "instance_id": instance_id,
        "lane_id": "default",
        "source_host": "Anil_NAS",
        "source_path": str(path),
        "snapshot_filename": "oem_radar.db",
        "child_as_of": {
            "query": "SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1",
            "clock": "NATIVE_RUN_COMPLETED_AT",
        },
        "schema_version": {"query": "SELECT value FROM metadata WHERE name='schema'"},
        "child_source_revision": None,
        "child_deployed_revision": "a" * 40,
        "child_deployed_revision_evidence": "verified image revision label",
        "unavailable_reasons": {"child_source_revision": "SOURCE_REVISION_NOT_EXPOSED"},
        "freshness_horizon": {
            "max_age_seconds": max_age,
            "clock": "NATIVE_RUN_COMPLETED_AT",
            "source": "owner-approved test horizon",
        },
        "adapter_package_sha": "b" * 40,
        "adapter_artifact_sha256": "c" * 64,
        "adapter_package_version": "0.1.0",
        "observer_contract_version": "0.2",
    }


def _spec(source: Path, **lane_overrides) -> dict:
    lane = _lane(source)
    lane.update(lane_overrides)
    return {
        "snapshot_contract_version": "1.0",
        "allowed_source_roots": [str(source.parent)],
        "lanes": [lane],
    }


def test_wal_consistent_backup_and_source_nonmutation(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    writer = _source(source)
    try:
        before = (_digest(source), _digest(Path(str(source) + "-wal")))
        output = tmp_path / "snapshots"
        output.mkdir()
        manifest_path, manifest = snapshot.produce(_spec(source), output)
        record = manifest["lanes"][0]
        assert record["refresh_outcome"] == "SUCCESS"
        assert record["freshness_state"] == "FRESH"
        assert record["child_execution_freshness"] == "FRESH"
        assert record["source_total_changes"] == 0
        assert record["integrity_result"] == {
            "sqlite_integrity": "ok", "foreign_key_violations": 0,
        }
        assert record["schema_version"] == "7"
        assert record["child_as_of_clock"] == "NATIVE_RUN_COMPLETED_AT"
        assert record["snapshot_path"].startswith("/app/real-state/")
        assert record["snapshot_path"] == "/app/real-state/oem_radar.db"
        copy = Path(record["snapshot_host_path"])
        assert copy.parent == manifest_path.parent
        assert _digest(copy) == record["snapshot_sha256"]
        assert copy.stat().st_size == record["snapshot_bytes"]
        with sqlite3.connect("file:" + copy.as_posix() + "?mode=ro", uri=True) as con:
            assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert before == (_digest(source), _digest(Path(str(source) + "-wal")))
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
        assert not list(manifest_path.parent.glob("*.partial"))
    finally:
        writer.close()


def test_recent_copy_cannot_freshen_old_native_run(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    writer = _source(source, finished_at=old)
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        _, manifest = snapshot.produce(_spec(source), output)
        row = manifest["lanes"][0]
        assert row["refresh_outcome"] == "SUCCESS"
        assert row["freshness_state"] == "FRESH"
        assert row["child_execution_freshness"] == "STALE"
        assert row["child_as_of"] == old.replace("+00:00", "Z")
    finally:
        writer.close()


def test_missing_source_is_recorded_unavailable_not_replaced_by_last_good(tmp_path: Path) -> None:
    source = tmp_path / "child" / "missing.db"
    source.parent.mkdir()
    output = tmp_path / "snapshots"
    output.mkdir()
    spec = _spec(source, last_good_snapshot_ref="sha256:" + "d" * 64)
    _, manifest = snapshot.produce(spec, output)
    row = manifest["lanes"][0]
    assert row["refresh_outcome"] == "UNAVAILABLE"
    assert row["freshness_state"] == "UNAVAILABLE"
    assert row["child_execution_freshness"] == "UNKNOWN"
    assert row["error_code"] == "SOURCE_UNAVAILABLE"
    assert row["snapshot_path"] is None and row["snapshot_sha256"] is None
    assert row["unavailable_reasons"]["snapshot_path"] == "SOURCE_UNAVAILABLE"
    assert row["last_good_snapshot_ref"] == "sha256:" + "d" * 64
    assert not list(output.rglob("*.db"))


@pytest.mark.parametrize("bad_source", ["corrupt_sqlite", "foreign_key_violation"])
def test_integrity_and_fk_fail_closed(tmp_path: Path, bad_source: str) -> None:
    source = tmp_path / "child" / "live.db"
    if bad_source == "corrupt_sqlite":
        source.parent.mkdir()
        source.write_bytes(b"not a SQLite database")
        writer = None
    else:
        writer = _source(source, bad_fk=True)
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        _, manifest = snapshot.produce(_spec(source), output)
        row = manifest["lanes"][0]
        assert row["refresh_outcome"] == "FAILED"
        assert row["freshness_state"] == "REFRESH_FAILED"
        assert row["child_execution_freshness"] == "UNKNOWN"
        assert row["snapshot_path"] is None
        assert row["snapshot_sha256"] is None
        if bad_source == "foreign_key_violation":
            assert row["error_code"] == "FOREIGN_KEY_CHECK_FAILED"
            assert row["integrity_result"]["foreign_key_violations"] == 1
        else:
            assert row["error_code"] == "SQLITE_REFRESH_FAILED"
        assert not list(output.rglob("*.db"))
    finally:
        if writer is not None:
            writer.close()


@pytest.mark.parametrize("change", [
    lambda s: s.update(snapshot_contract_version="2.0"),
    lambda s: s["lanes"].append(dict(s["lanes"][0])),
    lambda s: s["lanes"][0].update(lane_id="../escape"),
    lambda s: s["lanes"][0].update(snapshot_filename="../escape.db"),
    lambda s: s["lanes"][0].update(child_as_of={"query": "DELETE FROM runs", "clock": "NATIVE_RUN_COMPLETED_AT"}),
    lambda s: s["lanes"][0].update(observer_contract_version="0.3"),
    lambda s: s["lanes"][0].update(child_source_revision="unknown"),
])
def test_malformed_spec_rejected_before_any_output(tmp_path: Path, change) -> None:
    source = tmp_path / "child" / "live.db"
    source.parent.mkdir()
    output = tmp_path / "snapshots"
    output.mkdir()
    spec = _spec(source)
    change(spec)
    with pytest.raises(snapshot.SpecError):
        snapshot.produce(spec, output)
    assert list(output.iterdir()) == []


def test_null_native_evidence_has_explicit_reasons(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    writer = _source(source)
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        spec = _spec(source)
        spec["lanes"][0]["child_as_of"] = None
        spec["lanes"][0]["schema_version"] = None
        spec["lanes"][0]["unavailable_reasons"].update({
            "child_as_of": "NATIVE_CLOCK_UNAVAILABLE",
            "schema_version": "NO_SCHEMA_VERSION_TABLE",
        })
        _, manifest = snapshot.produce(spec, output)
        row = manifest["lanes"][0]
        assert row["refresh_outcome"] == "SUCCESS"
        assert row["freshness_state"] == "FRESH"
        assert row["child_execution_freshness"] == "UNKNOWN"
        assert row["child_as_of"] is None and row["child_as_of_clock"] is None
        assert row["schema_version"] is None
        assert row["unavailable_reasons"]["child_as_of_clock"] == "NATIVE_CLOCK_UNAVAILABLE"
    finally:
        writer.close()


def test_empty_native_run_query_has_null_clock_and_unknown_execution(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    writer = _source(source)
    writer.execute("DELETE FROM runs")
    writer.commit()
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        _, manifest = snapshot.produce(_spec(source), output)
        row = manifest["lanes"][0]
        assert row["refresh_outcome"] == "SUCCESS"
        assert row["child_as_of"] is None
        assert row["child_as_of_clock"] is None
        assert row["child_execution_freshness"] == "UNKNOWN"
        assert row["unavailable_reasons"]["child_as_of"] == "NO_NATIVE_RUN"
        assert row["unavailable_reasons"]["child_as_of_clock"] == "NO_NATIVE_RUN"
    finally:
        writer.close()


def test_atomic_manifest_and_unique_immutable_invocations(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    writer = _source(source)
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        first_path, first = snapshot.produce(_spec(source), output)
        first_hash = _digest(first_path)
        second_path, second = snapshot.produce(_spec(source), output)
        assert first_path != second_path
        assert first_path.parent != second_path.parent
        assert _digest(first_path) == first_hash
        assert first["lanes"][0]["snapshot_host_path"] != second["lanes"][0]["snapshot_host_path"]
        assert len(list(output.iterdir())) == 2
        assert not list(output.rglob("*.partial"))
        for path in (first_path, second_path):
            assert json.loads(path.read_text(encoding="utf-8"))["snapshot_contract_version"] == "1.0"
    finally:
        writer.close()


def test_multi_lane_manifest_keeps_failure_separate_from_success(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    writer = _source(source)
    try:
        output = tmp_path / "snapshots"
        output.mkdir()
        spec = _spec(source)
        second = _lane(tmp_path / "child" / "missing.db", instance_id="nas-soak")
        second["clank_id"] = "feature-phone-clank"
        second["snapshot_filename"] = "oem_radar_soak.db"
        spec["lanes"].append(second)
        _, manifest = snapshot.produce(spec, output)
        assert [row["refresh_outcome"] for row in manifest["lanes"]] == ["SUCCESS", "UNAVAILABLE"]
        assert manifest["lanes"][0]["snapshot_path"] == "/app/real-state/oem_radar.db"
        assert manifest["lanes"][1]["snapshot_path"] is None
        assert len(list(output.rglob("*.db"))) == 1
    finally:
        writer.close()


def test_duplicate_snapshot_filename_rejected_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    source.parent.mkdir()
    output = tmp_path / "snapshots"
    output.mkdir()
    spec = _spec(source)
    second = _lane(source, instance_id="nas-soak")
    second["clank_id"] = "feature-phone-clank"
    spec["lanes"].append(second)
    with pytest.raises(snapshot.SpecError, match="duplicate_snapshot_filename"):
        snapshot.produce(spec, output)
    assert list(output.iterdir()) == []


def test_multiple_active_lanes_for_one_clank_rejected(tmp_path: Path) -> None:
    source = tmp_path / "child" / "live.db"
    source.parent.mkdir()
    output = tmp_path / "snapshots"
    output.mkdir()
    spec = _spec(source)
    second = _lane(source, instance_id="nas-soak")
    second["snapshot_filename"] = "oem_radar_soak.db"
    spec["lanes"].append(second)
    with pytest.raises(snapshot.SpecError, match="multiple_active_lanes_for_clank_unsupported"):
        snapshot.produce(spec, output)
    assert list(output.iterdir()) == []


def test_cli_returns_manifest_path_and_rejects_bad_contract(tmp_path: Path, capsys) -> None:
    source = tmp_path / "child" / "missing.db"
    source.parent.mkdir()
    output = tmp_path / "snapshots"
    output.mkdir()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(source)), encoding="utf-8")
    assert snapshot.main(["--spec", str(spec_path), "--output-root", str(output)]) == 0
    good_out = capsys.readouterr()
    assert json.loads(Path(good_out.out.strip()).read_text(encoding="utf-8"))["lanes"][0]["refresh_outcome"] == "UNAVAILABLE"
    bad = _spec(source)
    bad["snapshot_contract_version"] = "2.0"
    spec_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        snapshot.main(["--spec", str(spec_path), "--output-root", str(output)])
    assert exc.value.code == 2
    rejected_out = capsys.readouterr()
    assert rejected_out.out == ""
    assert "incompatible_snapshot_contract_version" in rejected_out.err
