#!/usr/bin/env python3
"""COPS-000081 production candidate: verify one governed five-child copy set.

This is development source, not the installed task-14 gate. It reads immutable snapshot
copies and metadata only. In preflight mode it writes one exclusive, root-only
checksum file for exit-time input verification. In harvest-truth mode it reads
Motherclank's new derived snapshot record without altering it.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


OUTPUT_ROOT = Path("/volume2/clank/motherclank/state/inputs-cops-000081")
IMAGE_ID = "sha256:ae085407975001318efb923b9c122ad28b51f1a22a7bb06a4d433ed14bcc0529"
ADAPTER_SHA = "fbf286b45594280506c7c00ad54162259a82e0c8"
LANES = {
    "chinese-tech-wire": ("ctw-nas-canonical", "canonical", "/volume2/clank/chinese-tech-wire/state/ctw.db", "chinese_tech_wire.db", None, 10800),
    "korean-tech-wire": ("ktw-nas-canonical", "canonical", "/volume2/clank/korean-tech-wire/state/korean_tech_wire.db", "korean_tech_wire.db", "6", 10800),
    "semiconductor-intelligence": ("si-nas-experimental-tier-b", "experimental-tier-b", "/volume2/clank/semiconductor-intelligence/state/semi_intel.db", "semiconductor_intelligence.db", "c7d8e9f0a1b2", 10800),
    "feature-phone-clank": ("feature-phone-nas-experimental", "experimental", "/volume2/clank/feature-phone-clank/state/feature_phone_clank.db", "feature_phone_clank.db", "7", 36000),
    "oem-radar": ("oem-nas-canonical", "canonical", "/volume2/clank/oem-radar/canonical-cops-000072/state/radar.db", "radar.db", "7", 10800),
}


def fail(reason):
    raise RuntimeError(reason)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def parse_utc(value):
    if not isinstance(value, str):
        fail("missing_utc_timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("invalid_utc_timestamp")
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        fail("non_utc_timestamp")
    return stamp


def bare_sha(value):
    if not isinstance(value, str):
        fail("missing_sha256")
    bare = value[7:] if value.startswith("sha256:") else value
    if not re.fullmatch(r"[0-9a-f]{64}", bare):
        fail("malformed_sha256")
    return bare


def plain_regular(path):
    return path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1



def validate_attempt_binding(row, instance, lane, source, filename, copy):
    """Failure evidence has no current paths; never mislabel it as binding drift.

    Envelope/contract validation remains a separate step. This production gate
    rejects failed/unavailable attempts, so no old artifact reaches harvest.
    Successful copies still need their exact pinned identity/path bindings.
    """
    cid = row["clank_id"]
    outcome = row.get("refresh_outcome")
    if outcome != "SUCCESS":
        if outcome == "FAILED" and row.get("freshness_state") == "REFRESH_FAILED":
            fail("REFRESH_FAILED:" + cid)
        if outcome == "UNAVAILABLE" and row.get("freshness_state") == "UNAVAILABLE":
            fail("REFRESH_UNAVAILABLE:" + cid)
        fail("invalid_refresh_outcome:" + cid)
    if (row.get("instance_id"), row.get("lane_id"), row.get("source_path"),
            row.get("snapshot_path"), row.get("snapshot_host_path")) != (
                instance, lane, source, "/app/real-state/" + filename, str(copy)):
        fail("copy_binding_mismatch:" + cid)
    if (row.get("freshness_state"), row.get("source_total_changes")) != ("FRESH", 0):
        fail("unsuccessful_or_stale_copy:" + cid)


def load_inputs(args):
    manifest = Path(args.manifest)
    parent = manifest.parent
    if (parent.parent != OUTPUT_ROOT
            or not parent.name.startswith("snapshot-v1-")
            or manifest.name != "manifest.json"):
        fail("manifest_outside_pinned_output_root")
    for path in (OUTPUT_ROOT, parent, manifest, Path(args.inventory), Path(args.registry)):
        if path.is_symlink() or not path.exists():
            fail("missing_or_symlinked_input:" + str(path))
    if OUTPUT_ROOT.stat().st_uid != 10001 or not OUTPUT_ROOT.is_dir():
        fail("production_snapshot_root_owner_drift")
    if not plain_regular(manifest):
        fail("unsafe_manifest")
    inventory = Path(args.inventory)
    registry = Path(args.registry)
    if not plain_regular(inventory) or not plain_regular(registry):
        fail("unsafe_config_file")
    if digest(inventory) != bare_sha(args.inventory_sha) or digest(registry) != bare_sha(args.registry_sha):
        fail("configuration_hash_mismatch")
    inventory_text = inventory.read_text(encoding="utf-8")
    if ("inventory_status: INVENTORY_INCOMPLETE" not in inventory_text
            or "board_admission: BLOCKED_SEPARATE_COPS-000074" not in inventory_text
            or "status: NAS_MOTHERCLANK_TASK14_SCOPED_INVENTORY" not in inventory_text):
        fail("production_inventory_boundary_not_truthful")
    registry_data = json.loads(registry.read_text(encoding="utf-8"))
    if registry_data.get("extend_builtin") is not False or set(registry_data) != set(LANES) | {"extend_builtin"}:
        fail("registry_lane_set_mismatch")
    if {cid for cid in LANES if registry_data[cid].get("qc") is True} != {"korean-tech-wire"}:
        fail("ktw_must_be_sole_qc_lane")
    for cid, (instance, lane, _source, filename, schema, _horizon) in LANES.items():
        row = registry_data[cid]
        if (row.get("instance_id"), row.get("lane_id"), row.get("db")) != (instance, lane, filename):
            fail("registry_binding_mismatch:" + cid)
        if (None if schema is None else str(row.get("expected_schema_version"))) != schema:
            fail("registry_schema_mismatch:" + cid)

    doc = json.loads(manifest.read_text(encoding="utf-8"))
    if doc.get("snapshot_contract_version") != "1.0":
        fail("wrong_snapshot_contract")
    now = datetime.now(timezone.utc)
    observed = parse_utc(doc.get("observed_at"))
    if not -300 <= (now - observed).total_seconds() <= 1800:
        fail("snapshot_manifest_not_fresh_at_intake")
    if not -300 <= now.timestamp() - manifest.stat().st_mtime <= 1800:
        fail("snapshot_manifest_mtime_not_fresh_at_intake")
    rows = doc.get("lanes")
    if not isinstance(rows, list) or len(rows) != 5 or {row.get("clank_id") for row in rows} != set(LANES):
        fail("manifest_lane_set_mismatch")

    copies = []
    start = parse_utc(args.start_utc)
    for row in rows:
        cid = row["clank_id"]
        instance, lane, source, filename, schema, horizon = LANES[cid]
        copy = parent / filename
        validate_attempt_binding(row, instance, lane, source, filename, copy)
        if row.get("integrity_result") != {"sqlite_integrity": "ok", "foreign_key_violations": 0}:
            fail("copy_integrity_failed:" + cid)
        if row.get("freshness_horizon", {}).get("max_age_seconds") != horizon:
            fail("freshness_horizon_drift:" + cid)
        if (None if schema is None else str(row.get("schema_version"))) != schema:
            fail("schema_drift:" + cid)
        if row.get("adapter_package_sha") != ADAPTER_SHA or bare_sha(row.get("adapter_artifact_sha256")) != bare_sha(IMAGE_ID):
            fail("adapter_identity_drift:" + cid)
        if not plain_regular(copy) or copy.stat().st_size != row.get("snapshot_bytes") or digest(copy) != bare_sha(row.get("snapshot_sha256")):
            fail("copy_bytes_drift:" + cid)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(copy) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                fail("copy_sidecar_present:" + cid)
        with copy.open("rb") as stream:
            if stream.read(20)[18:20] != bytes((1, 1)):
                fail("copy_not_delete_journal_mode:" + cid)
        if cid in ("korean-tech-wire", "oem-radar"):
            if row.get("child_execution_freshness") != "FRESH" or row.get("child_as_of") is None:
                fail("proof_pair_native_freshness_failed:" + cid)
        if cid == "oem-radar" and parse_utc(row["child_as_of"]) < start:
            fail("oem_natural_run_not_newer_than_task14_start")
        if cid == "semiconductor-intelligence" and (row.get("child_as_of") is not None or row.get("child_execution_freshness") != "UNKNOWN"):
            fail("si_native_clock_caveat_lost")
        if cid == "korean-tech-wire":
            con = sqlite3.connect("file:" + copy.as_posix() + "?mode=ro&immutable=1", uri=True)
            try:
                con.execute("SELECT COUNT(*) FROM article_feedback").fetchone()
            finally:
                con.close()
        copies.append(copy)
    return manifest, inventory, registry, copies


def preflight(args):
    manifest, inventory, registry, copies = load_inputs(args)
    sums = Path(args.sums_file)
    if sums.parent.is_symlink() or not sums.parent.is_dir() or sums.exists() or sums.is_symlink():
        fail("unsafe_checksum_destination")
    fd = os.open(str(sums), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for path in (manifest, inventory, registry, *copies):
            stream.write(digest(path) + "  " + str(path) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"status": "PRODUCTION_INPUTS_VERIFIED", "manifest_sha256": digest(manifest),
                      "snapshots": len(copies), "registry_sha256": digest(registry),
                      "inventory_sha256": digest(inventory)}, sort_keys=True))


def harvest_truth(args):
    manifest_sha = "sha256:" + digest(Path(args.manifest))
    var = Path(args.var)
    if var.is_symlink() or not var.is_dir() or not (var / "snapshots").is_dir():
        fail("unsafe_live_var")
    matches = []
    for path in (var / "snapshots").glob("*.jsonl"):
        if not plain_regular(path):
            fail("unsafe_live_snapshot_record")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("snapshot_manifest_sha256") == manifest_sha:
                    matches.append(row)
    if len(matches) != 1:
        fail("expected_one_governed_harvest_for_new_manifest")
    row = matches[0]
    if (row.get("snapshot_contract_version") != "1.0"
            or row.get("inventory_sha256") != "sha256:" + bare_sha(args.inventory_sha)
            or row.get("adapter_registry_source_sha256") != "sha256:" + bare_sha(args.registry_sha)):
        fail("harvest_provenance_mismatch")
    clanks = row.get("clanks") or {}
    if set(clanks) != set(LANES):
        fail("harvest_lane_set_mismatch")
    states = {cid: block.get("snapshot_provenance", {}).get("effective_freshness_state")
              for cid, block in clanks.items()}
    if any(states[cid] != "FRESH" or clanks[cid].get("observation")
           for cid in ("korean-tech-wire", "oem-radar")):
        fail("ktw_oem_effective_fresh_pair_failed")
    if states["chinese-tech-wire"] != "UNKNOWN" or states["semiconductor-intelligence"] != "UNKNOWN":
        fail("ctw_si_unknown_boundary_lost")
    print(json.dumps({"status": "HARVEST_TRUTH_VERIFIED", "manifest_sha256": manifest_sha,
                      "effective_freshness": states}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "harvest-truth"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--inventory-sha", required=True)
    parser.add_argument("--registry-sha", required=True)
    parser.add_argument("--inventory")
    parser.add_argument("--registry")
    parser.add_argument("--start-utc")
    parser.add_argument("--sums-file")
    parser.add_argument("--var")
    args = parser.parse_args()
    if args.mode == "preflight":
        if not all((args.inventory, args.registry, args.start_utc, args.sums_file)):
            parser.error("preflight needs inventory, registry, start-utc, sums-file")
        preflight(args)
    else:
        if not args.var:
            parser.error("harvest-truth needs var")
        harvest_truth(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as error:
        print("motherclank_task14_manifest_gate_failed:" + str(error), file=sys.stderr)
        sys.exit(1)
