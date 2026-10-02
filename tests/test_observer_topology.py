"""Governed old/new inventories: membership comes from policy, not cardinality."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from motherclank.observer_topology import Topology, TopologyError, read_inventory

FIXTURES = Path(__file__).parent / "fixtures/observer-topology"
ADAPTER = "0770dd5f15be8a4a89bc43e5dd9644674d6683c0"
IMAGE = "sha256:" + "c" * 64


def governed_case(board=False):
    inventory = yaml.safe_load((FIXTURES / "legacy-inventory.yaml").read_text())
    spec = json.loads((FIXTURES / "legacy-spec.json").read_text())
    registry = json.loads((FIXTURES / "legacy-registry.json").read_text())
    for lane in spec["lanes"]:
        lane.update(adapter_package_sha=ADAPTER, adapter_artifact_sha256=IMAGE)
    if board:
        lane = copy.deepcopy(spec["lanes"][1])
        lane.update(clank_id="board-clank", instance_id="board-clank-nas-shadow", lane_id="shadow",
                    source_path="/volume2/clank/board-clank/state/board-clank.sqlite",
                    snapshot_filename="board_clank.sqlite", schema_version={"query": "PRAGMA user_version"},
                    child_deployed_revision="613c3a13c0e52088eeb33d6266b6ca7b2b783000")
        spec["lanes"].append(lane)
        spec["allowed_source_roots"].append("/volume2/clank/board-clank/state")
        registry["board-clank"] = dict(module="clank_fleet.adapters.board_clank", class_="BoardClankAdapter",
            db="board_clank.sqlite", instance_id=lane["instance_id"], lane_id="shadow", qc=False,
            expected_schema_version="3")
        registry["board-clank"]["class"] = registry["board-clank"].pop("class_")
        inventory["deployments"].append(dict(repository="board-clank", instance_id=lane["instance_id"],
            location_type="NAS", deployment_state="EXPERIMENTAL", deployed_commit_sha=lane["child_deployed_revision"],
            database_path=lane["source_path"]))
    lanes = {lane["clank_id"]: lane for lane in spec["lanes"]}
    for deployment in inventory["deployments"]:
        cid = deployment["repository"]
        deployment["observer"] = dict(
            transport="sealed-feature-phone-publication" if cid == "feature-phone-clank" else "sqlite-online-backup",
            registry=copy.deepcopy(registry[cid]), snapshot_spec=copy.deepcopy(lanes.get(cid)),
            expected_schema_version=registry[cid].get("expected_schema_version"))
    inventory["observer_policy"] = dict(version="1.0", adapter_revision=ADAPTER, image=IMAGE,
        observer_contract_version="0.2", snapshot_contract_version="1.0", qc_children=["korean-tech-wire"])
    return inventory, spec, registry


@pytest.mark.parametrize("board", [False, True])
def test_exact_governed_topology(board):
    inventory, spec, registry = governed_case(board)
    before = copy.deepcopy((inventory, spec, registry))
    policy = Topology(inventory, adapter_sha=ADAPTER, image_id=IMAGE)
    policy.validate_spec(spec)
    policy.validate_registry(registry)
    assert len(policy.children) == (6 if board else 5)
    assert ("board-clank" in policy.children) == board
    assert {cid for cid, row in policy.children.items() if row["observer"]["registry"]["qc"]} == {"korean-tech-wire"}
    assert (inventory, spec, registry) == before


@pytest.mark.parametrize("mutation", [
    lambda i, s, r: s["lanes"].pop(),
    lambda i, s, r: s["lanes"].append(dict(s["lanes"][0], clank_id="unregistered-seventh")),
    lambda i, s, r: s["lanes"].append(copy.deepcopy(s["lanes"][0])),
    lambda i, s, r: s["lanes"][0].update(lane_id="wrong"),
    lambda i, s, r: s["lanes"][0].update(schema_version="wrong"),
    lambda i, s, r: s["lanes"][0].update(adapter_package_sha="e" * 40),
    lambda i, s, r: s["lanes"][0].update(adapter_artifact_sha256="e" * 64),
    lambda i, s, r: s["allowed_source_roots"].append(s["allowed_source_roots"][0]),
    lambda i, s, r: r["board-clank"].update(qc=True),
    lambda i, s, r: r["board-clank"].update(module="arbitrary.module"),
    lambda i, s, r: r["board-clank"].update(expected_schema_version="4"),
    lambda i, s, r: r.update(extend_builtin=True),
])
def test_input_deviation_fails_closed(mutation):
    inventory, spec, registry = governed_case(True)
    policy = Topology(inventory, adapter_sha=ADAPTER, image_id=IMAGE)
    mutation(inventory, spec, registry)
    with pytest.raises(TopologyError):
        policy.validate_spec(spec)
        policy.validate_registry(registry)


@pytest.mark.parametrize("mutation", [
    lambda i: i["deployments"].append(copy.deepcopy(i["deployments"][0])),
    lambda i: i["deployments"][1].update(database_path=i["deployments"][0]["database_path"]),
    lambda i: i["deployments"][1].update(instance_id=i["deployments"][0]["instance_id"]),
    lambda i: i["deployments"][0]["observer"]["registry"].update(qc=True),
    lambda i: i["deployments"][1]["observer"]["registry"].update(qc=False),
    lambda i: i["deployments"][0]["observer"].update(expected_schema_version="wrong"),
    lambda i: i["observer_policy"].update(adapter_revision="e" * 40),
    lambda i: i["observer_policy"].update(image="sha256:" + "e" * 64),
])
def test_inventory_duplicates_and_policy_drift_rejected(mutation):
    inventory, _, _ = governed_case(True)
    mutation(inventory)
    with pytest.raises(TopologyError):
        Topology(inventory, adapter_sha=ADAPTER, image_id=IMAGE)


def test_sibling_configuration_and_publication_binding_unchanged():
    old, _, oldreg = governed_case(False)
    new, _, newreg = governed_case(True)
    assert new["deployments"][:-1] == old["deployments"]
    assert {cid: newreg[cid] for cid in oldreg} == oldreg
    policy = Topology(new, adapter_sha=ADAPTER, image_id=IMAGE)
    row = {"snapshot_path": "/app/feature-phone-accepted/export-existing/feature_phone_clank.db"}
    newreg["feature-phone-clank"]["db"] = row["snapshot_path"]
    policy.validate_registry(newreg, row)
    assert row == {"snapshot_path": newreg["feature-phone-clank"]["db"]}


@pytest.mark.parametrize("raw", ['{"schema_version":"2.0","schema_version":"2.0"}',
    'schema_version: "2.0"\nschema_version: "2.0"\n', 'base: &b {a: 1}\nx: {<<: *b}\n'])
def test_duplicate_keys_and_yaml_merge_rejected(tmp_path, raw):
    path = tmp_path / "inventory.yaml"
    path.write_text(raw)
    with pytest.raises(TopologyError):
        read_inventory(path)
