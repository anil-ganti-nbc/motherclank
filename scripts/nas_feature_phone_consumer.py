"""Governed SQLite backups plus sealed Feature Phone intake.

The owner supplies an authenticated child publication. This NON-ROOT container
has only inventory-governed canonical SQLite sources RO, accepted publications
RO, and one new Motherclank output directory RW. It has no Feature Phone
canonical or staging mount, Docker socket, publisher capability or delivery.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from motherclank.feature_phone_export import translate_failure, translate_publication
from motherclank.snapshot_manifest import load_manifest
from motherclank.observer_topology import Topology, TopologyError
from nas_snapshot_v1 import produce

ADAPTER_SHA = "0770dd5f15be8a4a89bc43e5dd9644674d6683c0"
FP_ID = "feature-phone-clank"
ACCEPTED = Path("/app/feature-phone-accepted")
FORBIDDEN_MOUNTS = (
    "/volume2/clank/feature-phone-clank/state", "/publication", "/app/data", "/export",
    "/var/run/docker.sock", "/run/docker.sock",
)


class IntakeError(ValueError):
    """Only fixed, secret-safe stage codes escape this orchestration."""


def require(value, code):
    if not value:
        raise IntakeError(code)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def read_ro_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "UNSAFE_INPUT_FILE")
    require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 10001, 0o440),
            "UNSEALED_INPUT_FILE")
    require(not any(parent.is_symlink() for parent in path.parents), "SYMLINKED_INPUT_PARENT")
    require(bool(os.statvfs(str(path)).f_flag & os.ST_RDONLY), "INPUT_NOT_KERNEL_RO")
    require(info.st_size <= 1024 * 1024, "INPUT_SIZE_LIMIT")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_FIELD")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    require(isinstance(value, dict), "INPUT_OBJECT_REQUIRED")
    return value


def runtime_boundary(output, sources):
    require(os.name == "posix" and (os.geteuid(), os.getegid()) == (10001, 10001),
            "PINNED_NONROOT_REQUIRED")
    mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    points = [line.split()[4] for line in mounts]
    # These names must be individual RO mounts, not a broad volume/share bind.
    # The owner-side Docker inspection also verifies host Sources; mountpoints
    # alone cannot attest an arbitrarily aliased host bind.
    roots = {item[0] for item in sources.values()}
    require(roots.issubset(set(points)), "EXACT_SOURCE_MOUNTS_REQUIRED")
    require(not any((p == "/volume2" or p.startswith("/volume2/")) and p not in roots
                    for p in points), "BROAD_OR_UNAPPROVED_NAS_MOUNT")
    require(not any(p == forbidden or p.startswith(forbidden + "/")
                    for p in points for forbidden in FORBIDDEN_MOUNTS),
            "FORBIDDEN_FEATURE_PHONE_OR_COMMAND_MOUNT")
    require(not any("feature-phone-clank/observer-export/staging" in p
                    or "feature-phone-clank/observer-export/failed" in p for p in points),
            "PRIVATE_EXPORT_MOUNT_FORBIDDEN")
    status = Path("/proc/self/status").read_text(encoding="utf-8")
    cap = re.search(r"^CapEff:\s*([0-9a-fA-F]+)$", status, re.M)
    require(cap is not None and int(cap.group(1), 16) == 0, "EFFECTIVE_CAPABILITIES_PRESENT")
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(39, 0, 0, 0, 0) == 1, "NO_NEW_PRIVILEGES_REQUIRED")
    require(bool(os.statvfs("/").f_flag & os.ST_RDONLY), "CONTAINER_ROOT_NOT_RO")
    for root, filename in sources.values():
        directory, db = Path(root), Path(root) / filename
        require(directory.is_dir() and not directory.is_symlink()
                and db.is_file() and not db.is_symlink(), "UNSAFE_SOURCE")
        require(bool(os.statvfs(root).f_flag & os.ST_RDONLY)
                and bool(os.statvfs(str(db)).f_flag & os.ST_RDONLY), "SOURCE_NOT_KERNEL_RO")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(db) + suffix)
            require(not sidecar.is_symlink(), "SOURCE_SIDECAR_SYMLINK")
            if sidecar.exists():
                require(bool(os.statvfs(str(sidecar)).f_flag & os.ST_RDONLY),
                        "SOURCE_SIDECAR_NOT_KERNEL_RO")
    require(output.is_dir() and not output.is_symlink()
            and not bool(os.statvfs(str(output)).f_flag & os.ST_RDONLY), "UNSAFE_OUTPUT_ROOT")
    print(f"consumer_input_boundary=PASS canonical_ro_sources={len(sources)} feature_phone_canonical_mount=false",
          file=sys.stderr)


def write_new_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def combine(manifest_path, doc, fp_record, registry, topology):
    """All input sets are private until the owner checks producer exit."""
    topology.validate_rows(doc["lanes"], include_publication=False)
    topology.validate_registry(registry)
    # Preserve every other adapter binding/configuration byte semantically.
    fp_registry = dict(registry[FP_ID])
    fp_registry.update(instance_id=fp_record["instance_id"], lane_id=fp_record["lane_id"],
                       db=(fp_record["snapshot_path"] or
                           "/app/feature-phone-accepted/unavailable/feature_phone_clank.db"))
    registry = dict(registry)
    registry[FP_ID] = fp_registry
    result = dict(doc)
    result["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result["lanes"] = list(doc["lanes"]) + [fp_record]
    topology.validate_rows(result["lanes"])
    topology.validate_registry(registry, fp_record)
    previous = manifest_path.with_name("sqlite-source-manifest.json")
    require(not previous.exists() and not previous.is_symlink(), "SQLITE_EVIDENCE_EXISTS")
    os.rename(manifest_path, previous)
    write_new_json(manifest_path, result)
    write_new_json(manifest_path.with_name("adapter-registry.json"), registry)
    load_manifest(manifest_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--request-context", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True,
                        help="Sealed JSON inventory (also valid YAML); never inferred from snapshots")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args(argv)
    try:
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id), "INVALID_IMAGE_ID")
        spec, receipt = read_ro_json(args.spec), read_ro_json(args.receipt)
        context, registry = read_ro_json(args.request_context), read_ro_json(args.registry)
        topology = Topology(read_ro_json(args.inventory), adapter_sha=ADAPTER_SHA, image_id=args.image_id)
        topology.validate_spec(spec)
        topology.validate_registry(registry)
        runtime_boundary(args.output_root, topology.sources)
        require(set(context) == {"request_id", "request_started_at", "prior_attempt_id",
                                 "receipt_sha256"}, "REQUEST_CONTEXT_SHAPE")
        require(digest(args.receipt) == context["receipt_sha256"], "REQUEST_RECEIPT_HASH_MISMATCH")
        observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        kwargs = dict(request_id=context["request_id"], request_started_at=context["request_started_at"],
                      prior_attempt_id=context["prior_attempt_id"], adapter_package_sha=ADAPTER_SHA,
                      adapter_artifact_sha256=args.image_id, adapter_package_version="0.0.1.dev0",
                      observed_at=observed)
        if receipt.get("status") == "SUCCESS":
            fp_record = translate_publication(
                ACCEPTED, receipt,
                last_good_snapshot_ref=receipt.get("last_good_snapshot_ref"), **kwargs)
        else:
            fp_record = translate_failure(receipt, last_good_snapshot_ref=receipt.get("last_good_snapshot_ref"),
                                          **kwargs)
        manifest, doc = produce(spec, args.output_root)
        result = combine(manifest, doc, fp_record, registry, topology)
        print(json.dumps({"status": "GOVERNED_CHILD_MANIFEST_READY", "manifest": str(manifest),
                          "manifest_sha256": digest(manifest),
                          "registry": str(manifest.with_name("adapter-registry.json")),
                          "registry_sha256": digest(manifest.with_name("adapter-registry.json")),
                          "feature_phone_refresh_outcome": fp_record["refresh_outcome"],
                          "feature_phone_execution_freshness": fp_record["child_execution_freshness"],
                          "lanes": len(result["lanes"])}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # No path, raw SQL, source content or untrusted exception text emitted.
        code = str(exc) if isinstance(exc, (IntakeError, TopologyError)) else "CONSUMER_INPUT_CONTRACT_REJECTED"
        print("feature_phone_consumer_error=" + code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
