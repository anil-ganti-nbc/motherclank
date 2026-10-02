"""Proof gate keeps failed lane handling ahead of copy path validation."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from motherclank import feature_phone_export as fp
from test_feature_phone_export import ARGS, NOW, _failure
from test_observer_topology import governed_case, IMAGE
from motherclank.observer_topology import TopologyError

spec = importlib.util.spec_from_file_location(
    "sealed_proof_gate", Path(__file__).resolve().parents[1] / "scripts/nas_feature_phone_proof_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SQLITE_CHILDREN = {r['repository'] for r in governed_case()[0]['deployments']} - {fp.CLANK_ID}


@pytest.fixture
def proof(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "utc_now", lambda: NOW)
    # Portable unit fixture only; production canonical source remains pinned.
    monkeypatch.setattr(fp, "CANONICAL_SOURCE_PATH", str(tmp_path / "canonical.db"))
    failure = fp.translate_failure(_failure(ref="d" * 64),
                                  last_good_snapshot_ref="d" * 64, **ARGS)
    failure.update(adapter_package_sha=gate.ADAPTER_SHA, adapter_artifact_sha256=IMAGE,
                   source_path=gate.CANONICAL_SOURCE_PATH)
    rows = [failure]
    inventory_doc, spec_doc, reg = governed_case()
    for deployment in inventory_doc['deployments']:
        cid = deployment['repository']
        if cid == fp.CLANK_ID:
            continue
        observer = deployment['observer']
        lane_spec = observer['snapshot_spec']
        instance, lane, source, filename, schema = (deployment['instance_id'], reg[cid]['lane_id'],
            deployment['database_path'], reg[cid]['db'], observer['expected_schema_version'])
        rows.append(dict(clank_id=cid, adapter_package_sha=gate.ADAPTER_SHA,
                         adapter_artifact_sha256=IMAGE, source_host="Anil_NAS",
                         refresh_outcome="SUCCESS", instance_id=instance, lane_id=lane,
                         source_path=source, snapshot_path="/app/real-state/" + filename,
                         schema_version=schema, freshness_horizon=lane_spec['freshness_horizon'],
                         observer_contract_version='0.2', child_deployed_revision=deployment['deployed_commit_sha']))
    manifest, registry, inventory = [tmp_path / name for name in
                                     ("manifest.json", "registry.json", "inventory.yaml")]
    registry.write_text(json.dumps(reg), encoding="utf-8")
    inventory.write_text(json.dumps(inventory_doc), encoding='utf-8')
    inventory.with_name('spec.json').write_text(json.dumps(spec_doc), encoding='utf-8')
    monkeypatch.setattr(gate, "load_manifest", lambda p: {"lanes": rows})
    monkeypatch.setattr(gate, "os", SimpleNamespace(geteuid=lambda: 10001, getegid=lambda: 10001))
    opened = []
    monkeypatch.setattr(gate, "ro_file", lambda p: opened.append(p))
    verified = []
    monkeypatch.setattr(gate, "verify_copy", lambda r, p: verified.append(r["clank_id"]))
    return manifest, registry, inventory, rows, opened, verified


def test_failed_export_admitted_no_historical_db_read_four_continue(proof):
    manifest, registry, inventory, rows, opened, verified = proof
    checked, inputs = gate.validate_inputs(manifest, registry, inventory, IMAGE, "FAILED", inventory.with_name("spec.json"))
    assert checked[fp.CLANK_ID]["last_good_snapshot_ref"] == "d" * 64
    assert set(verified) == SQLITE_CHILDREN
    assert len(inputs) == 4 + len(SQLITE_CHILDREN)
    assert not any("feature-phone-accepted" in str(p) for p in opened)


@pytest.mark.parametrize("change", [
    {"snapshot_path": "/app/feature-phone-accepted/old/feature_phone_clank.db"},
    {"freshness_state": "FRESH"}, {"child_execution_freshness": "FRESH"},
    {"instance_id": "feature-phone-nas-experimental"},
])
def test_failed_export_cannot_be_promoted_or_identity_aliased(proof, change):
    manifest, registry, inventory, rows, *_ = proof
    rows[0].update(change)
    with pytest.raises((gate.ProofError, fp.FeaturePhoneExportError, TopologyError)):
        gate.validate_inputs(manifest, registry, inventory, IMAGE, "FAILED", inventory.with_name("spec.json"))


def test_proof_pair_or_config_integrity_failure_is_fatal(proof):
    manifest, registry, inventory, rows, *_ = proof
    rows[1]["refresh_outcome"] = "FAILED"
    with pytest.raises(gate.ProofError, match="OTHER_CHILD_REFRESH_FAILED"):
        gate.validate_inputs(manifest, registry, inventory, IMAGE, "FAILED", inventory.with_name("spec.json"))


def test_failed_refresh_not_misdiagnosed_as_copy_binding(proof):
    manifest, registry, inventory, rows, *_ = proof
    with pytest.raises(gate.ProofError, match="FAILED_EXPORT_NOT_TRUTHFUL") as error:
        gate.validate_inputs(manifest, registry, inventory, IMAGE, "SUCCESS", inventory.with_name("spec.json"))
    assert "copy_binding_mismatch" not in str(error.value)


def test_failed_export_registry_cannot_point_to_old_success(proof):
    manifest, registry, inventory, *_ = proof
    reg = json.loads(registry.read_text())
    reg[fp.CLANK_ID]["db"] = "/app/feature-phone-accepted/old/feature_phone_clank.db"
    registry.write_text(json.dumps(reg), encoding="utf-8")
    with pytest.raises(TopologyError, match="EXACT_REGISTRY_MAPPING_DRIFT"):
        gate.validate_inputs(manifest, registry, inventory, IMAGE, "FAILED", inventory.with_name("spec.json"))


@pytest.mark.parametrize("state,linked,reject", [
    ("UNKNOWN", "current", False), ("HEALTHY", "current", True),
    ("UNKNOWN", "old", True),
])
def test_m1_failed_export_never_promoted_and_exact_m0_link_required(tmp_path, state, linked, reject):
    directory = tmp_path / "syntheses"
    directory.mkdir()
    states = {cid: "FRESH" for cid in SQLITE_CHILDREN}
    states[fp.CLANK_ID] = "UNKNOWN"
    claims = {cid: {"state": "HEALTHY"} for cid in SQLITE_CHILDREN}
    claims[fp.CLANK_ID] = {"state": state}
    (directory / "proof.jsonl").write_text(json.dumps({
        "snapshot_hash": linked, "content_hash": "m1-current", "clanks": claims}) + "\n",
        encoding="utf-8")
    truth = {"snapshot_content_hash": "current", "expected_feature_phone": "FAILED",
             "effective_freshness": states, "governed_children": sorted(states)}
    if reject:
        with pytest.raises(gate.ProofError):
            gate.synthesis_truth(tmp_path, truth)
    else:
        assert gate.synthesis_truth(tmp_path, truth)["states"][fp.CLANK_ID] == "UNKNOWN"
