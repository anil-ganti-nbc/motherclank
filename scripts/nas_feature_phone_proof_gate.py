"""Isolated sealed-export proof gate; never installs or runs task 14.

Run in the exact non-root candidate with accepted-only and snapshot inputs RO.
An ordinary failed Feature Phone refresh is admitted BEFORE successful-path
binding; it has no current copy, and no previous accepted DB is opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from motherclank.feature_phone_export import (
    ACCEPTED_ROOT, CLANK_ID, INSTANCE_ID, LANE_ID, CANONICAL_SOURCE_PATH,
    validate_lineage,
)
from motherclank.snapshot_manifest import load_manifest, verify_copy
from motherclank.observer_topology import Topology, read_inventory, unique_pairs

ADAPTER_SHA = "0770dd5f15be8a4a89bc43e5dd9644674d6683c0"


class ProofError(ValueError):
    pass


def require(value, code):
    if not value:
        raise ProofError(code)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def bare(value):
    require(isinstance(value, str), "MISSING_HASH")
    value = value.removeprefix("sha256:")
    require(re.fullmatch(r"[0-9a-f]{64}", value), "INVALID_HASH")
    return value


def ro_file(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not any(p.is_symlink() for p in path.parents), "UNSAFE_INPUT")
    require(os.statvfs(str(path)).f_flag & os.ST_RDONLY, "INPUT_NOT_KERNEL_RO")


def validate_inputs(manifest, registry, inventory, image_id, expected_fp, spec):
    require((os.geteuid(), os.getegid()) == (10001, 10001), "NONROOT_REQUIRED")
    for path in (manifest, registry, inventory, spec):
        ro_file(path)
    topology = Topology(read_inventory(inventory), adapter_sha=ADAPTER_SHA, image_id=image_id)
    topology.validate_spec(json.loads(spec.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs))
    doc = load_manifest(manifest)
    topology.validate_rows(doc["lanes"])
    rows = {row["clank_id"]: row for row in doc["lanes"]}
    reg = json.loads(registry.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    topology.validate_registry(reg, rows[CLANK_ID])
    inputs = [manifest, registry, inventory, spec]
    for cid, row in rows.items():
        require(row["adapter_package_sha"] == ADAPTER_SHA
                and bare(row["adapter_artifact_sha256"]) == bare(image_id), "ADAPTER_IDENTITY_DRIFT")
        require(row["source_host"] == "Anil_NAS", "SOURCE_HOST_DRIFT")
        if cid == CLANK_ID:
            require((row["instance_id"], row["lane_id"], row["source_path"])
                    == (INSTANCE_ID, LANE_ID, CANONICAL_SOURCE_PATH), "FEATURE_PHONE_IDENTITY_DRIFT")
            require((reg[cid].get("instance_id"), reg[cid].get("lane_id"),
                     str(reg[cid].get("expected_schema_version"))) == (INSTANCE_ID, LANE_ID, "7"),
                    "FEATURE_PHONE_REGISTRY_IDENTITY_DRIFT")
            lineage = validate_lineage(row["child_export"], record=row)
            require(row["freshness_horizon"]["max_age_seconds"] == 36000,
                    "FEATURE_PHONE_HORIZON_DRIFT")
            # Preserve e93195e ordering: absent current copy is normal failure
            # evidence, not successful-copy path drift. Do not inspect last-good.
            if row["refresh_outcome"] != "SUCCESS":
                require(expected_fp == "FAILED" and row["refresh_outcome"] == "FAILED"
                        and row["freshness_state"] == "REFRESH_FAILED"
                        and row["child_execution_freshness"] == "UNKNOWN"
                        and all(row[k] is None for k in ("snapshot_path", "snapshot_sha256", "snapshot_bytes")),
                        "FAILED_EXPORT_NOT_TRUTHFUL")
                require(reg[cid].get("db") == "/app/feature-phone-accepted/unavailable/feature_phone_clank.db",
                        "FAILED_EXPORT_REGISTRY_POINTS_TO_OLD_COPY")
                continue
            require(expected_fp == "SUCCESS", "EXPECTED_FAILURE_FIXTURE")
            copy = ACCEPTED_ROOT / lineage["attempt_id"] / "feature_phone_clank.db"
            require(row["snapshot_path"] == str(copy), "SEALED_COPY_BINDING_DRIFT")
            for filename, key in (("metadata.json", "metadata_sha256"),
                                  ("publication.json", "publication_sha256")):
                path = copy.parent / filename
                ro_file(path)
                require(digest(path) == bare(lineage[key]), "SEALED_METADATA_HASH_DRIFT")
                inputs.append(path)
        else:
            configured = topology.children[cid]
            binding = configured["observer"]
            spec_row = binding["snapshot_spec"]
            instance, lane, source, filename, schema = (
                configured["instance_id"], binding["registry"]["lane_id"], configured["database_path"],
                spec_row["snapshot_filename"], binding["expected_schema_version"])
            require(row["refresh_outcome"] == "SUCCESS", "OTHER_CHILD_REFRESH_FAILED")
            require((row["instance_id"], row["lane_id"], row["source_path"], row["snapshot_path"])
                    == (instance, lane, source, "/app/real-state/" + filename), "OTHER_CHILD_BINDING_DRIFT")
            require((reg[cid]["instance_id"], reg[cid]["lane_id"], reg[cid]["db"])
                    == (instance, lane, filename), "OTHER_REGISTRY_DRIFT")
            require((None if reg[cid].get("expected_schema_version") is None
                     else str(reg[cid]["expected_schema_version"])) == schema,
                    "OTHER_REGISTRY_SCHEMA_DRIFT")
            require((None if schema is None else str(row["schema_version"])) == schema,
                    "OTHER_SCHEMA_DRIFT")
            require(row["freshness_horizon"] == spec_row["freshness_horizon"], "OTHER_HORIZON_DRIFT")
            copy = manifest.parent / filename
        require(reg[cid]["db"] == (str(copy) if cid == CLANK_ID else copy.name),
                "ADAPTER_COPY_PATH_DRIFT")
        ro_file(copy)
        verify_copy(row, copy)
        inputs.append(copy)
    return rows, inputs


def harvest_truth(var, manifest, registry, inventory, expected_fp):
    target = "sha256:" + digest(manifest)
    matches = []
    for path in (var / "snapshots").glob("*.jsonl"):
        require(not path.is_symlink(), "UNSAFE_DERIVED_RECORD")
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("snapshot_manifest_sha256") == target:
                matches.append(record)
    require(len(matches) == 1, "ONE_CURRENT_HARVEST_REQUIRED")
    record = matches[0]
    require(record.get("inventory_sha256") == "sha256:" + digest(inventory)
            and record.get("adapter_registry_source_sha256") == "sha256:" + digest(registry),
            "DERIVED_PROVENANCE_DRIFT")
    clanks = record.get("clanks", {})
    expected = {row["repository"] for row in read_inventory(inventory)["deployments"]}
    require(set(clanks) == expected, "DERIVED_LANE_SET_DRIFT")
    states = {cid: block.get("snapshot_provenance", {}).get("effective_freshness_state")
              for cid, block in clanks.items()}
    require(all(states[cid] == "FRESH" and not clanks[cid].get("observation")
                for cid in ("korean-tech-wire", "oem-radar") if cid in expected), "PROOF_PAIR_NOT_FRESH")
    require(all(states[cid] == "UNKNOWN" for cid in ("chinese-tech-wire", "semiconductor-intelligence") if cid in expected),
            "HISTORICAL_UNKNOWN_BOUNDARY_LOST")
    fp = clanks[CLANK_ID]
    if expected_fp == "FAILED":
        require(states[CLANK_ID] == "UNKNOWN" and fp.get("observation") == "SNAPSHOT_REFRESH_FAILED"
                and fp.get("snapshot_provenance", {}).get("refresh_outcome") == "FAILED",
                "NEGATIVE_FIXTURE_PROMOTED_OR_MISDIAGNOSED")
    else:
        allowed_observations = {
            "FRESH": {None},
            "STALE": {"SNAPSHOT_STALE", "CHILD_EXECUTION_STALE"},
            "UNKNOWN": {"CHILD_EXECUTION_UNKNOWN"},
        }
        require(fp.get("observation") in allowed_observations.get(states[CLANK_ID], set()),
                "SEALED_CHILD_ADAPTER_FAILED")
    return {"status": "HARVEST_TRUTH_PASS", "expected_feature_phone": expected_fp,
            "effective_freshness": states, "manifest_sha256": target,
            "snapshot_content_hash": record["content_hash"],
            "governed_children": sorted(expected), "governed_children_continued_at_m0": True}


def synthesis_truth(var, truth):
    """Bind M1 to this request's M0, not an older healthy synthesis record."""
    matches = []
    for path in (var / "syntheses").glob("*.jsonl"):
        require(not path.is_symlink(), "UNSAFE_SYNTHESIS_RECORD")
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if value.get("snapshot_hash") == truth["snapshot_content_hash"]:
                matches.append(value)
    require(len(matches) == 1, "ONE_CURRENT_SYNTHESIS_REQUIRED")
    claims = matches[0].get("clanks", {})
    require(set(claims) == set(truth["governed_children"]), "SYNTHESIS_LANE_SET_DRIFT")
    for cid, claim in claims.items():
        require(claim.get("state") in {"HEALTHY", "DEGRADED", "FAILED", "UNKNOWN"},
                "SYNTHESIS_STATE_INVALID")
        if truth["effective_freshness"][cid] != "FRESH":
            require(claim["state"] == "UNKNOWN" and not claim.get("evidence_derived_claims"),
                    "NONCURRENT_EVIDENCE_PROMOTED_AT_M1")
    if truth["expected_feature_phone"] == "FAILED":
        require(claims[CLANK_ID]["state"] == "UNKNOWN", "FAILED_EXPORT_SYNTHESIZED_HEALTHY")
    return {"status": "SYNTHESIS_TRUTH_PASS", "snapshot_content_hash": truth["snapshot_content_hash"],
            "synthesis_content_hash": matches[0]["content_hash"],
            "states": {cid: claim["state"] for cid, claim in claims.items()},
            "full_pipeline_process_exits_still_required": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "harvest-truth", "synthesis-truth"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--expected-feature-phone", choices=("SUCCESS", "FAILED"), required=True)
    parser.add_argument("--var", type=Path)
    args = parser.parse_args()
    try:
        _, inputs = validate_inputs(args.manifest, args.registry, args.inventory,
                                    args.image_id, args.expected_feature_phone, args.spec)
        if args.mode == "preflight":
            result = {"status": "SEALED_GOVERNED_INPUTS_PASS", "expected_feature_phone": args.expected_feature_phone,
                      "input_hashes": {str(path): digest(path) for path in inputs}}
        else:
            require(args.var is not None, "VAR_REQUIRED")
            result = harvest_truth(args.var, args.manifest, args.registry, args.inventory,
                                   args.expected_feature_phone)
            if args.mode == "synthesis-truth":
                result = synthesis_truth(args.var, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("sealed_feature_phone_proof_gate=CONTRACT_REJECTED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
