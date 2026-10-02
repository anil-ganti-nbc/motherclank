"""Strict governed observer inventory; never discover membership from inputs.

The root-owned deployment package authenticates inventory bytes. This module
then binds registry/spec/manifest inputs to that inventory. It deliberately
contains no fleet cardinality and no Board-specific membership logic.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path, PurePosixPath


FP = "feature-phone-clank"
QC = "korean-tech-wire"


class TopologyError(ValueError):
    """Bounded, secret-safe configuration failure."""


def require(ok, code):
    if not ok:
        raise TopologyError(code)


def unique_pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "DUPLICATE_CONFIG_KEY")
        result[key] = value
    return result


def _mapping(loader, node):
    require(not any(key.value == "<<" for key, _ in node.value), "YAML_MERGE_FORBIDDEN")
    return unique_pairs((loader.construct_object(key, deep=True),
                         loader.construct_object(value, deep=True)) for key, value in node.value)


def read_inventory(path):
    import yaml
    class UniqueLoader(yaml.SafeLoader):
        pass
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    raw = Path(path).read_bytes()
    require(len(raw) <= 1024 * 1024, "INVENTORY_SIZE_LIMIT")
    try:
        return yaml.load(raw, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise TopologyError("INVENTORY_PARSE_FAILED") from None


def _path(value):
    require(type(value) is str and value.startswith("/volume2/clank/")
            and "\\" not in value and "," not in value
            and ".." not in PurePosixPath(value).parts
            and str(PurePosixPath(value)) == value, "CANONICAL_PATH_INVALID")
    return value


def bare(value):
    return value[7:] if isinstance(value, str) and value.startswith("sha256:") else value


def _sha(value, length):
    require(type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value),
            "SOURCE_OR_ARTIFACT_PIN_INVALID")


class Topology:
    def __init__(self, inventory, *, adapter_sha, image_id):
        require(type(inventory) is dict and inventory.get("schema_version") == "2.0",
                "INVENTORY_SCHEMA_DRIFT")
        policy = inventory.get("observer_policy")
        require(type(policy) is dict and set(policy) == {
            "version", "adapter_revision", "image", "observer_contract_version",
            "snapshot_contract_version", "qc_children"}, "OBSERVER_POLICY_REQUIRED")
        _sha(adapter_sha, 40)
        require(type(image_id) is str and image_id.startswith("sha256:"), "IMAGE_PIN_INVALID")
        _sha(image_id[7:], 64)
        require(policy == dict(version="1.0", adapter_revision=adapter_sha, image=image_id,
                              observer_contract_version="0.2", snapshot_contract_version="1.0",
                              qc_children=[QC]), "OBSERVER_POLICY_PIN_DRIFT")
        deployments = inventory.get("deployments")
        require(type(deployments) is list and bool(deployments), "GOVERNED_CHILDREN_REQUIRED")
        self.children = {}
        instances, stores, filenames = set(), set(), set()
        for deployment in deployments:
            require(type(deployment) is dict, "DEPLOYMENT_OBJECT_REQUIRED")
            cid = deployment.get("repository")
            require(type(cid) is str and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", cid), "CHILD_ID_INVALID")
            require(cid not in self.children, "DUPLICATE_CHILD_IDENTITY")
            instance = deployment.get("instance_id")
            require(type(instance) is str and bool(instance) and instance not in instances,
                    "DUPLICATE_OR_INVALID_INSTANCE")
            instances.add(instance)
            source = _path(deployment.get("database_path"))
            require(source not in stores, "DUPLICATE_STORE_IDENTITY")
            stores.add(source)
            _sha(deployment.get("deployed_commit_sha"), 40)
            require(deployment.get("location_type") == "NAS", "SOURCE_HOST_DRIFT")
            observer = deployment.get("observer")
            require(type(observer) is dict and set(observer) == {
                "transport", "registry", "snapshot_spec", "expected_schema_version"},
                "GOVERNED_OBSERVER_BINDING_REQUIRED")
            reg = observer["registry"]
            require(type(reg) is dict and set(reg) <= {"module", "class", "db", "instance_id",
                "lane_id", "qc", "expected_schema_version"}, "REGISTRY_MAPPING_INVALID")
            require(all(type(reg.get(k)) is str and bool(reg[k]) for k in
                        ("module", "class", "db", "instance_id", "lane_id")), "REGISTRY_MAPPING_INVALID")
            require(reg["instance_id"] == instance and type(reg.get("qc")) is bool
                    and reg["qc"] == (cid == QC), "REGISTRY_IDENTITY_OR_QC_DRIFT")
            schema = observer["expected_schema_version"]
            require(schema is None or (type(schema) is str and bool(schema)), "SCHEMA_POLICY_INVALID")
            require(reg.get("expected_schema_version") == schema, "REGISTRY_SCHEMA_DRIFT")
            lane = observer["snapshot_spec"]
            if cid == FP:
                require(observer["transport"] == "sealed-feature-phone-publication" and lane is None,
                        "FEATURE_PHONE_TRANSPORT_DRIFT")
                require(reg["db"] == "/app/feature-phone-accepted/unavailable/feature_phone_clank.db",
                        "FEATURE_PHONE_BASE_REGISTRY_DRIFT")
            else:
                require(observer["transport"] == "sqlite-online-backup" and type(lane) is dict,
                        "SQLITE_TRANSPORT_REQUIRED")
                require((lane.get("clank_id"), lane.get("instance_id"), lane.get("lane_id"),
                         lane.get("source_path"), lane.get("child_deployed_revision")) ==
                        (cid, instance, reg["lane_id"], source, deployment["deployed_commit_sha"]),
                        "SNAPSHOT_IDENTITY_DRIFT")
                require(lane.get("source_host") == "Anil_NAS"
                        and lane.get("observer_contract_version") == "0.2"
                        and lane.get("adapter_package_sha") == adapter_sha
                        and bare(lane.get("adapter_artifact_sha256")) == image_id[7:],
                        "SNAPSHOT_SOURCE_PIN_DRIFT")
                filename = lane.get("snapshot_filename")
                require(type(filename) is str and re.fullmatch(r"[a-zA-Z0-9_-]+\.(?:db|sqlite)", filename)
                        and filename not in filenames and reg["db"] == filename,
                        "DUPLICATE_OR_INVALID_SNAPSHOT_STORE")
                filenames.add(filename)
            self.children[cid] = copy.deepcopy(deployment)
        require(FP in self.children and QC in self.children, "REQUIRED_TRANSPORT_OR_QC_ABSENT")
        self.adapter_sha, self.image_id = adapter_sha, image_id

    @property
    def sqlite_children(self):
        return {cid: row for cid, row in self.children.items() if cid != FP}

    @property
    def sources(self):
        return {cid: (str(PurePosixPath(row["database_path"]).parent),
                      PurePosixPath(row["database_path"]).name)
                for cid, row in self.sqlite_children.items()}

    def validate_spec(self, spec):
        require(type(spec) is dict and set(spec) == {"snapshot_contract_version", "allowed_source_roots", "lanes"}
                and spec["snapshot_contract_version"] == "1.0", "SNAPSHOT_CONTRACT_DRIFT")
        lanes = spec["lanes"]
        require(type(lanes) is list and all(type(row) is dict for row in lanes), "LANE_OBJECT_REQUIRED")
        keys = [row.get("clank_id") for row in lanes]
        require(len(keys) == len(set(keys)) and set(keys) == set(self.sqlite_children), "GOVERNED_SOURCE_SET_DRIFT")
        require(all(row == self.children[row["clank_id"]]["observer"]["snapshot_spec"] for row in lanes),
                "EXACT_SNAPSHOT_SPEC_DRIFT")
        roots = spec["allowed_source_roots"]
        require(type(roots) is list and all(type(x) is str for x in roots)
                and len(roots) == len(set(roots)) and set(roots) == {v[0] for v in self.sources.values()},
                "CANONICAL_ROOT_SET_DRIFT")

    def validate_registry(self, registry, fp_record=None):
        expected = {cid: copy.deepcopy(row["observer"]["registry"]) for cid, row in self.children.items()}
        expected["extend_builtin"] = False
        if fp_record is not None:
            expected[FP]["db"] = fp_record["snapshot_path"] or expected[FP]["db"]
        require(type(registry) is dict and registry.get("extend_builtin") is False
                and all(type(registry.get(cid)) is dict and type(registry[cid].get("qc")) is bool
                        for cid in self.children)
                and registry == expected, "EXACT_REGISTRY_MAPPING_DRIFT")

    def validate_rows(self, rows, *, include_publication=True):
        require(type(rows) is list and all(type(row) is dict for row in rows), "MANIFEST_ROWS_INVALID")
        keys = [row.get("clank_id") for row in rows]
        wanted = self.children if include_publication else self.sqlite_children
        require(len(keys) == len(set(keys)) and set(keys) == set(wanted), "GOVERNED_MANIFEST_SET_DRIFT")
        for row in rows:
            configured = self.children[row["clank_id"]]
            reg = configured["observer"]["registry"]
            require((row.get("instance_id"), row.get("lane_id"), row.get("source_path"),
                     row.get("child_deployed_revision"), row.get("source_host")) ==
                    (configured["instance_id"], reg["lane_id"], configured["database_path"],
                     configured["deployed_commit_sha"], "Anil_NAS"), "MANIFEST_IDENTITY_DRIFT")
            require(row.get("adapter_package_sha") == self.adapter_sha
                    and bare(row.get("adapter_artifact_sha256")) == self.image_id[7:]
                    and row.get("observer_contract_version") == "0.2", "MANIFEST_PIN_DRIFT")
