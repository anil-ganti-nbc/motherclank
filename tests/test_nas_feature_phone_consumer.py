"""Four-source orchestration and sealed Feature Phone request binding.

Kernel boundary tests use synthetic mount/stat facts. They do not claim a
Windows fixture is a Linux RO bind; actual NAS isolation is a separate gate.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from motherclank import adapters, cli, feature_phone_export as fp, qc_corpus, synthesis
from motherclank.snapshot_manifest import load_manifest
from motherclank.observer_topology import Topology, TopologyError
from test_observer_topology import governed_case

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
IMAGE_ID = "sha256:" + "c" * 64


@pytest.fixture
def consumer(monkeypatch):
    # NAS provenance is a POSIX path, even when hermetic tests run on Windows.
    # Actual temp-file I/O retains the native Path implementation.
    from motherclank import snapshot_manifest
    monkeypatch.setattr(snapshot_manifest, "Path", lambda value: (
        PurePosixPath(value) if str(value).startswith("/volume2/") else Path(value)))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "nas_feature_phone_consumer_test", SCRIPTS / "nas_feature_phone_consumer.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_inventory, module.test_spec, module.test_registry = governed_case()
    module.test_topology = Topology(module.test_inventory, adapter_sha=module.ADAPTER_SHA, image_id=IMAGE_ID)
    # Test fixture compatibility names, deliberately not production globals.
    module.SOURCES = module.test_topology.sources
    return module


def _spec(module):
    return copy.deepcopy(module.test_spec)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _row(module, tmp_path, clank_id, *, failed=False):
    now = datetime.now(UTC) - timedelta(seconds=1)
    created = now - timedelta(seconds=5)
    row = {
        "snapshot_contract_version": "1.0", "clank_id": clank_id,
        "instance_id": fp.INSTANCE_ID if clank_id == fp.CLANK_ID else clank_id + "-nas",
        "lane_id": "experimental" if clank_id == fp.CLANK_ID else "production",
        "source_host": "Anil_NAS", "source_path": str(tmp_path / (clank_id + "-canonical.db")),
        "snapshot_created_at": _iso(created), "child_as_of": _iso(created - timedelta(seconds=5)),
        "child_as_of_clock": "NATIVE_RUN_ROW_UTC", "schema_version": "7",
        "child_source_revision": None, "child_deployed_revision": fp.DEPLOYED_REVISION,
        "snapshot_path": str(tmp_path / "accepted" / "attempt" / (clank_id + ".db")),
        "snapshot_sha256": "d" * 64, "snapshot_bytes": 4096,
        "integrity_result": {"sqlite_integrity": "ok", "foreign_key_violations": 0},
        "adapter_package_sha": module.ADAPTER_SHA, "adapter_artifact_sha256": IMAGE_ID,
        "adapter_package_version": "0.0.1.dev0", "observer_contract_version": "0.2",
        "refresh_outcome": "SUCCESS", "freshness_state": "FRESH",
        "child_execution_freshness": "FRESH", "observed_at": _iso(now),
        "freshness_horizon": {"max_age_seconds": 36000, "clock": "NATIVE_RUN_ROW_UTC",
                              "source": "owner-approved fixture horizon"},
        "error_code": None, "last_good_snapshot_ref": None,
        "unavailable_reasons": {"child_source_revision": "NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE"},
    }
    if failed:
        row.update(snapshot_created_at=None, child_as_of=None, child_as_of_clock=None,
                   schema_version=None, snapshot_path=None, snapshot_sha256=None, snapshot_bytes=None,
                   integrity_result={"sqlite_integrity": "unavailable", "foreign_key_violations": None},
                   refresh_outcome="FAILED", freshness_state="REFRESH_FAILED",
                   child_execution_freshness="UNKNOWN", error_code="EXPORT_LOCK_BUSY",
                   last_good_snapshot_ref="e" * 64)
        row["unavailable_reasons"].update({key: "NO_CURRENT_EXPORT_AFTER_LOCK" for key in (
            "snapshot_created_at", "child_as_of", "child_as_of_clock", "schema_version")})
    configured = module.test_topology.children[clank_id]
    binding = configured["observer"]
    row.update(instance_id=configured["instance_id"], lane_id=binding["registry"]["lane_id"],
               source_path=configured["database_path"], child_deployed_revision=configured["deployed_commit_sha"])
    return row


def _four_manifest(module, tmp_path):
    doc = {"snapshot_contract_version": "1.0", "observed_at": _iso(datetime.now(UTC)),
           "lanes": [_row(module, tmp_path, clank_id) for clank_id in module.SOURCES]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    return path, doc


def _registry(module):
    return copy.deepcopy(module.test_registry)


def test_strict_four_source_spec_excludes_feature_phone_canonical(consumer):
    spec = _spec(consumer)
    consumer.test_topology.validate_spec(spec)
    assert fp.CLANK_ID not in {row["clank_id"] for row in spec["lanes"]}
    assert not any("feature-phone" in root for root in spec["allowed_source_roots"])


@pytest.mark.parametrize("mutation,code", [
    (lambda s: s.update(snapshot_contract_version="0.2"), "SNAPSHOT_CONTRACT_DRIFT"),
    (lambda s: s["lanes"].append({"clank_id": fp.CLANK_ID}), "GOVERNED_SOURCE_SET_DRIFT"),
    (lambda s: s["lanes"].pop(), "GOVERNED_SOURCE_SET_DRIFT"),
    (lambda s: s["lanes"][0].update(clank_id=fp.CLANK_ID), "GOVERNED_SOURCE_SET_DRIFT"),
    (lambda s: s["allowed_source_roots"].append("/volume2/clank/feature-phone-clank/state"), "CANONICAL_ROOT_SET_DRIFT"),
    (lambda s: s["allowed_source_roots"].append("/volume2/clank"), "CANONICAL_ROOT_SET_DRIFT"),
    (lambda s: s["allowed_source_roots"].append(s["allowed_source_roots"][0]), "CANONICAL_ROOT_SET_DRIFT"),
    (lambda s: s["lanes"][0].update(source_path="/tmp/aliased.db"), "EXACT_SNAPSHOT_SPEC_DRIFT"),
    (lambda s: s["lanes"][0].update(adapter_package_sha="f" * 40), "EXACT_SNAPSHOT_SPEC_DRIFT"),
    (lambda s: s["lanes"][0].update(adapter_artifact_sha256="b" * 64), "EXACT_SNAPSHOT_SPEC_DRIFT"),
    (lambda s: s["lanes"][0].update(adapter_artifact_sha256=None), "EXACT_SNAPSHOT_SPEC_DRIFT"),
])
def test_spec_authority_drift_rejected_before_produce(consumer, mutation, code):
    spec = _spec(consumer)
    mutation(spec)
    with pytest.raises(TopologyError, match=code):
        consumer.test_topology.validate_spec(spec)


@pytest.mark.parametrize("failed", [False, True])
def test_combine_preserves_four_source_evidence_and_binds_only_accepted_copy(consumer, tmp_path, failed):
    path, doc = _four_manifest(consumer, tmp_path)
    original_bytes, original_doc = path.read_bytes(), copy.deepcopy(doc)
    registry = _registry(consumer)
    original_registry = copy.deepcopy(registry)
    row = _row(consumer, tmp_path, fp.CLANK_ID, failed=failed)
    result = consumer.combine(path, doc, row, registry, consumer.test_topology)
    emitted_registry = json.loads(path.with_name("adapter-registry.json").read_text(encoding="utf-8"))
    assert path.with_name("sqlite-source-manifest.json").read_bytes() == original_bytes
    assert doc == original_doc and registry == original_registry
    assert result["lanes"][:-1] == original_doc["lanes"]
    assert result["lanes"][-1] == row
    assert len(load_manifest(path)["lanes"]) == 5
    assert {key: emitted_registry[key] for key in consumer.SOURCES} == {
        key: original_registry[key] for key in consumer.SOURCES}
    assert emitted_registry[fp.CLANK_ID]["instance_id"] == fp.INSTANCE_ID
    assert emitted_registry[fp.CLANK_ID]["lane_id"] == fp.LANE_ID
    assert emitted_registry[fp.CLANK_ID]["qc"] is False
    if failed:
        assert row["snapshot_path"] is row["snapshot_sha256"] is row["snapshot_bytes"] is None
        assert row["last_good_snapshot_ref"] == "e" * 64
        assert row["child_execution_freshness"] == "UNKNOWN"
        assert emitted_registry[fp.CLANK_ID]["db"] == (
            "/app/feature-phone-accepted/unavailable/feature_phone_clank.db")
    else:
        assert Path(emitted_registry[fp.CLANK_ID]["db"]).is_absolute()
        assert emitted_registry[fp.CLANK_ID]["db"] == row["snapshot_path"]
        assert "canonical" not in emitted_registry[fp.CLANK_ID]["db"]


@pytest.mark.parametrize("bad_registry", ["builtin", "missing", "extra"])
def test_combine_rejects_unbounded_adapter_registry_without_replacing_manifest(consumer, tmp_path, bad_registry):
    path, doc = _four_manifest(consumer, tmp_path)
    before = path.read_bytes()
    registry = _registry(consumer)
    if bad_registry == "builtin":
        registry["extend_builtin"] = True
    elif bad_registry == "missing":
        registry.pop("oem-radar")
    else:
        registry["board-clank"] = {"db": "forbidden.db"}
    with pytest.raises(TopologyError, match="EXACT_REGISTRY_MAPPING_DRIFT"):
        consumer.combine(path, doc, _row(consumer, tmp_path, fp.CLANK_ID), registry, consumer.test_topology)
    assert path.read_bytes() == before
    assert not path.with_name("sqlite-source-manifest.json").exists()


def test_combine_never_overwrites_previous_four_source_evidence(consumer, tmp_path):
    path, doc = _four_manifest(consumer, tmp_path)
    previous = path.with_name("sqlite-source-manifest.json")
    previous.write_bytes(b"preserved earlier evidence")
    before = path.read_bytes()
    with pytest.raises(consumer.IntakeError, match="SQLITE_EVIDENCE_EXISTS"):
        consumer.combine(path, doc, _row(consumer, tmp_path, fp.CLANK_ID), _registry(consumer), consumer.test_topology)
    assert path.read_bytes() == before
    assert previous.read_bytes() == b"preserved earlier evidence"


def _failed_receipt():
    now = datetime.now(UTC)
    return {
        "request_format_version": "1.0", "status": "FAILED", "request_id": "fp-request-test-1234",
        "request_started_at": _iso(now - timedelta(seconds=20)),
        "request_completed_at": _iso(now - timedelta(seconds=10)),
        "attempt_id": None, "exporter_revision": fp.EXPORTER_REVISION, "export_image": fp.EXPORT_IMAGE,
        "deployed_revision": fp.DEPLOYED_REVISION, "artifact_sha256": None, "metadata_sha256": None,
        "publication_sha256": None, "snapshot_bytes": None, "publication_path": None,
        "error_code": "EXPORT_LOCK_BUSY", "error_stage": "LOCK", "last_good_snapshot_ref": "e" * 64,
    }


def _main_case(module, tmp_path, monkeypatch, receipt=None):
    receipt = receipt or _failed_receipt()
    files = {name: tmp_path / (name + ".json") for name in ("spec", "receipt", "context", "registry", "inventory")}
    values = {"spec": _spec(module), "receipt": receipt, "registry": _registry(module)}
    values["inventory"] = copy.deepcopy(module.test_inventory)
    files["receipt"].write_text(json.dumps(receipt), encoding="utf-8")
    values["context"] = {"request_id": receipt["request_id"], "request_started_at": receipt["request_started_at"],
                         "prior_attempt_id": "export-20260930T131948Z-d39b9e696dd74b76",
                         "receipt_sha256": hashlib.sha256(files["receipt"].read_bytes()).hexdigest()}
    monkeypatch.setattr(module, "runtime_boundary", lambda p, sources: None)
    monkeypatch.setattr(module, "read_ro_json", lambda p: copy.deepcopy(values[next(k for k, v in files.items() if v == p)]))
    argv = ["--spec", str(files["spec"]), "--receipt", str(files["receipt"]),
            "--request-context", str(files["context"]), "--registry", str(files["registry"]),
            "--output-root", str(tmp_path), "--image-id", IMAGE_ID, "--inventory", str(files["inventory"])]
    return values, files, argv


def test_main_failure_uses_real_typed_translation_then_continues_four_source_produce(consumer, tmp_path, monkeypatch, capsys):
    values, _, argv = _main_case(consumer, tmp_path, monkeypatch)
    # Metadata-only fixture path semantics; production executes strictly Linux.
    calls = []
    def produce(spec, output):
        calls.append((copy.deepcopy(spec), output))
        return _four_manifest(consumer, tmp_path)
    monkeypatch.setattr(consumer, "produce", produce)
    monkeypatch.setattr(consumer, "translate_publication", lambda *a, **k: pytest.fail("failure opened accepted evidence"))
    assert consumer.main(argv) == 0
    out = capsys.readouterr()
    assert calls == [(values["spec"], tmp_path)]
    summary = json.loads(out.out)
    assert summary["status"] == "GOVERNED_CHILD_MANIFEST_READY" and summary["lanes"] == 5
    assert summary["feature_phone_refresh_outcome"] == "FAILED"
    assert summary["feature_phone_execution_freshness"] == "UNKNOWN"
    failed = load_manifest(Path(summary["manifest"]))["lanes"][-1]
    assert failed["snapshot_path"] is None and failed["last_good_snapshot_ref"] == "e" * 64
    assert failed["child_export"]["request_id"] == values["context"]["request_id"]


def test_main_success_passes_independent_request_and_last_good_reference(consumer, tmp_path, monkeypatch, capsys):
    receipt = _failed_receipt()
    receipt.update(status="SUCCESS", error_code=None, error_stage=None)
    values, _, argv = _main_case(consumer, tmp_path, monkeypatch, receipt)
    seen = {}
    def translate(root, actual_receipt, **kwargs):
        seen.update(root=root, receipt=actual_receipt, kwargs=kwargs)
        return _row(consumer, tmp_path, fp.CLANK_ID)
    monkeypatch.setattr(consumer, "translate_publication", translate)
    monkeypatch.setattr(consumer, "translate_failure", lambda *a, **k: pytest.fail("success translated as failure"))
    monkeypatch.setattr(consumer, "produce", lambda spec, output: _four_manifest(consumer, tmp_path))
    assert consumer.main(argv) == 0
    assert seen["root"] == consumer.ACCEPTED
    assert seen["receipt"] == receipt
    assert seen["kwargs"]["request_id"] == values["context"]["request_id"]
    assert seen["kwargs"]["request_started_at"] == values["context"]["request_started_at"]
    assert seen["kwargs"]["prior_attempt_id"] == values["context"]["prior_attempt_id"]
    assert seen["kwargs"]["last_good_snapshot_ref"] == receipt["last_good_snapshot_ref"]
    assert seen["kwargs"]["adapter_package_sha"] == consumer.ADAPTER_SHA
    assert seen["kwargs"]["adapter_artifact_sha256"] == IMAGE_ID
    assert json.loads(capsys.readouterr().out)["feature_phone_refresh_outcome"] == "SUCCESS"


@pytest.mark.parametrize("kind,code", [("hash", "REQUEST_RECEIPT_HASH_MISMATCH"), ("shape", "REQUEST_CONTEXT_SHAPE")])
def test_request_context_and_receipt_hash_rejected_before_translation_or_source_read(consumer, tmp_path, monkeypatch, capsys, kind, code):
    values, _, argv = _main_case(consumer, tmp_path, monkeypatch)
    if kind == "hash":
        values["context"]["receipt_sha256"] = "f" * 64
    else:
        values["context"]["arbitrary_command"] = "DO_NOT_LOG_SECRET"
    monkeypatch.setattr(consumer, "translate_failure", lambda *a, **k: pytest.fail("unbound receipt translated"))
    monkeypatch.setattr(consumer, "produce", lambda *a, **k: pytest.fail("unbound receipt opened canonical source"))
    assert consumer.main(argv) == 2
    out = capsys.readouterr()
    assert out.out == "" and code in out.err and "DO_NOT_LOG" not in out.err


def test_failed_request_identity_mismatch_is_whole_input_rejection_before_four_backups(consumer, tmp_path, monkeypatch, capsys):
    values, _, argv = _main_case(consumer, tmp_path, monkeypatch)
    values["context"]["request_id"] = "different-owner-request"
    monkeypatch.setattr(consumer, "produce", lambda *a, **k: pytest.fail("wrong request opened canonical source"))
    assert consumer.main(argv) == 2
    out = capsys.readouterr()
    assert out.out == "" and "CONSUMER_INPUT_CONTRACT_REJECTED" in out.err


def test_ro_json_requires_kernel_readonly_and_preserves_file(consumer, tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    path.write_text('{"status":"FAILED"}', encoding="utf-8")
    before = path.read_bytes()
    flags = SimpleNamespace(f_flag=1)
    monkeypatch.setattr(consumer, "os", SimpleNamespace(statvfs=lambda p: flags, ST_RDONLY=1))
    class SealedFile:
        parents = path.parents
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_nlink=1, st_uid=0,
                                   st_gid=10001, st_size=path.stat().st_size)
        def __str__(self):
            return str(path)
        def read_text(self, **kwargs):
            return path.read_text(**kwargs)
    assert consumer.read_ro_json(SealedFile()) == {"status": "FAILED"}
    flags.f_flag = 0
    with pytest.raises(consumer.IntakeError, match="INPUT_NOT_KERNEL_RO"):
        consumer.read_ro_json(SealedFile())
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode,nlink,size,code", [
    (stat.S_IFLNK | 0o777, 1, 10, "UNSAFE_INPUT_FILE"),
    (stat.S_IFREG | 0o440, 2, 10, "UNSAFE_INPUT_FILE"),
    (stat.S_IFREG | 0o440, 1, 1024 * 1024 + 1, "INPUT_SIZE_LIMIT"),
])
def test_ro_json_rejects_symlink_hardlink_and_oversized_input(consumer, monkeypatch, mode, nlink, size, code):
    class File:
        parents = []
        def lstat(self):
            return SimpleNamespace(st_mode=mode, st_nlink=nlink, st_size=size, st_uid=0, st_gid=10001)
        def read_text(self, **kwargs):
            pytest.fail("unsafe metadata parsed")
        def __str__(self):
            return "/sealed/receipt.json"
    monkeypatch.setattr(consumer, "os", SimpleNamespace(statvfs=lambda p: SimpleNamespace(f_flag=1), ST_RDONLY=1))
    with pytest.raises(consumer.IntakeError, match=code):
        consumer.read_ro_json(File())


@pytest.mark.parametrize("raw,uid,gid,mode,parent_symlink,code", [
    ('{"a":1,"a":2}', 0, 10001, 0o440, False, "DUPLICATE_JSON_FIELD"),
    ('[]', 0, 10001, 0o440, False, "INPUT_OBJECT_REQUIRED"),
    ('{"a":1}', 10001, 10001, 0o440, False, "UNSEALED_INPUT_FILE"),
    ('{"a":1}', 0, 0, 0o440, False, "UNSEALED_INPUT_FILE"),
    ('{"a":1}', 0, 10001, 0o640, False, "UNSEALED_INPUT_FILE"),
    ('{"a":1}', 0, 10001, 0o440, True, "SYMLINKED_INPUT_PARENT"),
])
def test_ro_json_sealed_identity_strict_shape_and_duplicate_keys(consumer, monkeypatch, raw, uid, gid, mode, parent_symlink, code):
    class File:
        parents = [SimpleNamespace(is_symlink=lambda: parent_symlink)]
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_nlink=1, st_size=len(raw),
                                   st_uid=uid, st_gid=gid)
        def __str__(self):
            return "/sealed/receipt.json"
        def read_text(self, **kwargs):
            return raw
    monkeypatch.setattr(consumer, "os", SimpleNamespace(statvfs=lambda p: SimpleNamespace(f_flag=1), ST_RDONLY=1))
    with pytest.raises(consumer.IntakeError, match=code):
        consumer.read_ro_json(File())


def _boundary(consumer, monkeypatch, *, extra_mounts=(), omitted_roots=(), uid=10001, gid=10001,
              capabilities=0, nnp=1, writable=(), symlinks=(), sidecars=(), output_ro=False):
    roots = {root for root, _ in consumer.SOURCES.values()}
    dbs = {root + "/" + filename for root, filename in consumer.SOURCES.values()}
    mounts = ["/", "/proc", "/app/feature-phone-accepted", "/app/output", *(sorted(roots - set(omitted_roots))), *extra_mounts]
    mount_text = "\n".join(f"10 1 0:1 / {p} ro,nosuid - ext4 /dev/fake ro" for p in mounts)
    directories = roots | {"/", "/app/output", "/app/feature-phone-accepted"}
    files = dbs | set(sidecars)
    class BoundaryPath:
        def __init__(self, value):
            self.value = str(value)
        def __str__(self):
            return self.value
        def __truediv__(self, value):
            return BoundaryPath(self.value + "/" + str(value))
        def read_text(self, **kwargs):
            if self.value == "/proc/self/mountinfo":
                return mount_text
            if self.value == "/proc/self/status":
                return f"CapEff:\t{capabilities:016x}\n"
            pytest.fail("unexpected boundary file read " + self.value)
        def is_dir(self):
            return self.value in directories
        def is_file(self):
            return self.value in files
        def is_symlink(self):
            return self.value in symlinks
        def exists(self):
            return self.value in directories | files
    def statvfs(path):
        value = str(path)
        rw = value in writable or (value == "/app/output" and not output_ro)
        return SimpleNamespace(f_flag=0 if rw else 1)
    monkeypatch.setattr(consumer, "Path", BoundaryPath)
    monkeypatch.setattr(consumer, "os", SimpleNamespace(name="posix", geteuid=lambda: uid,
                                                       getegid=lambda: gid, statvfs=statvfs, ST_RDONLY=1))
    monkeypatch.setattr(consumer, "ctypes", SimpleNamespace(CDLL=lambda *a, **k: SimpleNamespace(prctl=lambda *a: nnp)))
    return BoundaryPath("/app/output")


def test_boundary_requires_exact_nonroot_ro_sources_caps_nnp_and_rw_isolated_output(consumer, monkeypatch, capsys):
    output = _boundary(consumer, monkeypatch)
    consumer.runtime_boundary(output, consumer.SOURCES)
    assert "canonical_ro_sources=4 feature_phone_canonical_mount=false" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs,code", [
    ({"uid": 0}, "PINNED_NONROOT_REQUIRED"),
    ({"gid": 0}, "PINNED_NONROOT_REQUIRED"),
    ({"extra_mounts": ("/volume2/clank",)}, "BROAD_OR_UNAPPROVED_NAS_MOUNT"),
    ({"extra_mounts": ("/volume2",)}, "BROAD_OR_UNAPPROVED_NAS_MOUNT"),
    ({"extra_mounts": (fp.CANONICAL_SOURCE_PATH.rsplit("/", 1)[0],)}, "BROAD_OR_UNAPPROVED_NAS_MOUNT"),
    ({"extra_mounts": ("/var/run/docker.sock",)}, "FORBIDDEN_FEATURE_PHONE_OR_COMMAND_MOUNT"),
    ({"extra_mounts": ("/publication",)}, "FORBIDDEN_FEATURE_PHONE_OR_COMMAND_MOUNT"),
    ({"extra_mounts": ("/export",)}, "FORBIDDEN_FEATURE_PHONE_OR_COMMAND_MOUNT"),
    ({"extra_mounts": ("/aliased/feature-phone-clank/observer-export/staging",)}, "PRIVATE_EXPORT_MOUNT_FORBIDDEN"),
    ({"extra_mounts": ("/aliased/feature-phone-clank/observer-export/failed",)}, "PRIVATE_EXPORT_MOUNT_FORBIDDEN"),
    ({"omitted_roots": ("/volume2/clank/oem-radar/canonical-cops-000072/state",)}, "EXACT_SOURCE_MOUNTS_REQUIRED"),
    ({"capabilities": 1}, "EFFECTIVE_CAPABILITIES_PRESENT"),
    ({"nnp": 0}, "NO_NEW_PRIVILEGES_REQUIRED"),
    ({"writable": ("/",)}, "CONTAINER_ROOT_NOT_RO"),
    ({"writable": ("/volume2/clank/chinese-tech-wire/state",)}, "SOURCE_NOT_KERNEL_RO"),
    ({"symlinks": ("/volume2/clank/korean-tech-wire/state/korean_tech_wire.db",)}, "UNSAFE_SOURCE"),
    ({"sidecars": ("/volume2/clank/korean-tech-wire/state/korean_tech_wire.db-wal",),
      "writable": ("/volume2/clank/korean-tech-wire/state/korean_tech_wire.db-wal",)}, "SOURCE_SIDECAR_NOT_KERNEL_RO"),
    ({"symlinks": ("/volume2/clank/korean-tech-wire/state/korean_tech_wire.db-shm",)}, "SOURCE_SIDECAR_SYMLINK"),
    ({"output_ro": True}, "UNSAFE_OUTPUT_ROOT"),
    ({"symlinks": ("/app/output",)}, "UNSAFE_OUTPUT_ROOT"),
])
def test_boundary_negative_cases_reject_without_any_source_sql(consumer, monkeypatch, kwargs, code):
    output = _boundary(consumer, monkeypatch, **kwargs)
    with pytest.raises(consumer.IntakeError, match=code):
        consumer.runtime_boundary(output, consumer.SOURCES)


def test_failed_export_full_isolated_pipeline_keeps_four_children_and_ktw_qc(consumer, tmp_path, monkeypatch, capsys):
    """Real local M0/M1/M2/M3/QC flow; only adapter methods are fixture-bound.

    The failed Feature Phone adapter is deliberately unreadable. Its retained
    last-good digest is provenance only and cannot be probed as current state.
    All manifest, registry, copy-integrity and governed-QC gates remain real.
    """
    from test_snapshot_manifest_v1 import _case, HealthyAdapter

    adapters.ensure_adapter_plane()
    template, manifest, inventory, built, _, _ = _case(tmp_path)
    rows, fixture_adapters = [], {}
    registry = _registry(consumer)
    for clank_id in consumer.SOURCES:
        row = copy.deepcopy(template)
        row.update(clank_id=clank_id, instance_id=clank_id + "-nas", lane_id="production",
                   adapter_package_sha=consumer.ADAPTER_SHA, adapter_artifact_sha256=IMAGE_ID)
        rows.append(row)
        fixture_adapters[clank_id] = HealthyAdapter(Path(row["snapshot_path"]), row["child_as_of"])
        registry[clank_id].update(db="copy.db", expected_schema_version=row["schema_version"])
    doc = {"snapshot_contract_version": "1.0", "observed_at": _iso(datetime.now(UTC)), "lanes": rows}
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    receipt = _failed_receipt()
    failure = fp.translate_failure(
        receipt, request_id=receipt["request_id"], request_started_at=receipt["request_started_at"],
        observed_at=_iso(datetime.now(UTC)), adapter_package_sha=consumer.ADAPTER_SHA,
        adapter_artifact_sha256=IMAGE_ID, adapter_package_version="0.0.1.dev0",
        prior_attempt_id="export-20260930T131948Z-d39b9e696dd74b76",
        last_good_snapshot_ref=receipt["last_good_snapshot_ref"])
    # This legacy pipeline fixture intentionally shares one synthetic SQLite
    # file between adapters. Build its manifest directly: the production
    # topology tests separately require distinct governed store identities.
    doc["lanes"].append(failure)
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    registry[fp.CLANK_ID]["db"] = "/app/feature-phone-accepted/unavailable/feature_phone_clank.db"
    manifest.with_name("adapter-registry.json").write_text(json.dumps(registry), encoding="utf-8")
    registry_path = manifest.with_name("adapter-registry.json")
    entries = json.loads(registry_path.read_text(encoding="utf-8"))
    entries.pop("extend_builtin")
    class NeverRead:
        db_path = tmp_path / "accepted-old-evidence-must-not-open.db"
        calls = 0
        def identity(self):
            self.calls += 1
            pytest.fail("failed export reached Feature Phone adapter")
    failed_adapter = NeverRead()
    fixture_adapters[fp.CLANK_ID] = failed_adapter
    built.update(
        adapters=fixture_adapters, versions={}, qc_adapters=["korean-tech-wire"],
        expected_identities={row["clank_id"]: {"instance_id": row["instance_id"], "lane_id": row["lane_id"]}
                             for row in rows + [failure]},
        expected_schema_versions={row["clank_id"]: row["schema_version"] for row in rows},
        registry_path=registry_path,
        registry_source_sha256="sha256:" + hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        registry_effective_sha256="sha256:" + hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        registry_entries=entries)
    monkeypatch.setattr(cli, "build_adapters", lambda *a, **k: built)
    inputs = [manifest, registry_path, inventory, Path(template["source_path"]), Path(template["snapshot_path"])]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    var = tmp_path / "detached-var"
    common = ["--real-state", str(tmp_path), "--inventory", str(inventory),
              "--adapter-registry", str(registry_path), "--snapshot-manifest", str(manifest),
              "--expected-adapter-package-sha", consumer.ADAPTER_SHA,
              "--expected-adapter-artifact-sha256", IMAGE_ID]
    assert cli.main(["harvest", *common, "--out", str(var)]) == 0
    for command in ("synthesize", "detect", "recommend"):
        assert cli.main([command, "--var-dir", str(var), "--out", str(var)]) == 0
    assert cli.main(["ingest-qc", *common, "--var-dir", str(var), "--out", str(var)]) == 0
    harvest = synthesis.read_latest_snapshot(var)
    assert harvest["clanks"][fp.CLANK_ID]["observation"] == "SNAPSHOT_REFRESH_FAILED"
    fp_provenance = harvest["clanks"][fp.CLANK_ID]["snapshot_provenance"]
    assert fp_provenance["snapshot_path"] is fp_provenance["snapshot_sha256"] is None
    assert fp_provenance["last_good_snapshot_ref"] == "e" * 64
    assert fp_provenance["effective_freshness_state"] == "UNKNOWN"
    assert fp_provenance["child_export"]["status"] == "FAILED"
    claims = synthesis.synthesize_fleet(harvest)["clanks"]
    assert claims[fp.CLANK_ID]["state"] == "UNKNOWN"
    assert all(claims[clank_id]["state"] == "HEALTHY" for clank_id in consumer.SOURCES)
    assert all(fixture_adapters[clank_id].calls > 0 for clank_id in consumer.SOURCES)
    assert failed_adapter.calls == 0
    qc_batch = qc_corpus.read_previous_qc_batch(var)
    assert qc_batch["record_count"] == 1
    assert set(qc_batch["corpus"]["clanks"]) == {"korean-tech-wire"}
    assert qc_batch["corpus"]["records"][0]["source_snapshot"]["clank_id"] == "korean-tech-wire"
    assert qc_batch["corpus"]["records"][0]["ingestion_snapshot_hash"] == harvest["content_hash"]
    assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs} == before
    output = capsys.readouterr()
    assert "copy_binding_mismatch" not in output.err and "governed QC rejected" not in output.err
    assert list((var / "syntheses").glob("*.jsonl"))
    assert list((var / "anomalies").glob("*.jsonl"))
    assert list((var / "recommendations").glob("*.jsonl"))
