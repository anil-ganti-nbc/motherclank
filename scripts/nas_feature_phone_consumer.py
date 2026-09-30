"""Isolated COPS-000081 four-child backup plus sealed Feature Phone intake.

The owner host requests/publishes a child export first. This NON-ROOT container
has four unchanged canonical sources RO, accepted Feature Phone publications RO,
and one new Motherclank output directory RW. It has no Feature Phone canonical
or staging mount, Docker socket, publisher capability, collection or delivery.
No changes to the existing task-14 launcher are authorized by this script.
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
from nas_snapshot_v1 import produce

ADAPTER_SHA = "fbf286b45594280506c7c00ad54162259a82e0c8"
FP_ID = "feature-phone-clank"
ACCEPTED = Path("/app/feature-phone-accepted")
SOURCES = {
    "chinese-tech-wire": ("/volume2/clank/chinese-tech-wire/state", "ctw.db"),
    "korean-tech-wire": ("/volume2/clank/korean-tech-wire/state", "korean_tech_wire.db"),
    "semiconductor-intelligence": ("/volume2/clank/semiconductor-intelligence/state", "semi_intel.db"),
    "oem-radar": ("/volume2/clank/oem-radar/canonical-cops-000072/state", "radar.db"),
}
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


def validate_four_spec(spec, image_id):
    require(isinstance(spec, dict), "SPEC_OBJECT_REQUIRED")
    require(spec.get("snapshot_contract_version") == "1.0", "SNAPSHOT_CONTRACT_DRIFT")
    lanes = spec.get("lanes")
    require(isinstance(lanes, list) and len(lanes) == 4, "FOUR_SOURCE_SET_REQUIRED")
    require(all(isinstance(lane, dict) for lane in lanes), "LANE_OBJECT_REQUIRED")
    require({x.get("clank_id") for x in lanes} == set(SOURCES), "FOUR_SOURCE_SET_DRIFT")
    require(isinstance(spec.get("allowed_source_roots"), list)
            and len(spec["allowed_source_roots"]) == 4
            and set(spec["allowed_source_roots"]) == {x[0] for x in SOURCES.values()},
            "CANONICAL_ROOT_SET_DRIFT")
    for lane in lanes:
        root, filename = SOURCES[lane["clank_id"]]
        require(lane.get("source_path") == root + "/" + filename, "CANONICAL_PATH_DRIFT")
        require(isinstance(lane.get("adapter_artifact_sha256"), str), "ADAPTER_IDENTITY_DRIFT")
        require(lane.get("adapter_package_sha") == ADAPTER_SHA
                and lane.get("adapter_artifact_sha256", "").removeprefix("sha256:")
                == image_id.removeprefix("sha256:"), "ADAPTER_IDENTITY_DRIFT")


def runtime_boundary(output):
    require(os.name == "posix" and (os.geteuid(), os.getegid()) == (10001, 10001),
            "PINNED_NONROOT_REQUIRED")
    mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    points = [line.split()[4] for line in mounts]
    # These names must be individual RO mounts, not a broad volume/share bind.
    # The owner-side Docker inspection also verifies host Sources; mountpoints
    # alone cannot attest an arbitrarily aliased host bind.
    roots = {item[0] for item in SOURCES.values()}
    require(roots.issubset(set(points)), "EXACT_FOUR_SOURCE_MOUNTS_REQUIRED")
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
    for root, filename in SOURCES.values():
        directory, db = Path(root), Path(root) / filename
        require(directory.is_dir() and not directory.is_symlink()
                and db.is_file() and not db.is_symlink(), "UNSAFE_FOUR_SOURCE")
        require(bool(os.statvfs(root).f_flag & os.ST_RDONLY)
                and bool(os.statvfs(str(db)).f_flag & os.ST_RDONLY), "FOUR_SOURCE_NOT_KERNEL_RO")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(db) + suffix)
            require(not sidecar.is_symlink(), "FOUR_SOURCE_SIDECAR_SYMLINK")
            if sidecar.exists():
                require(bool(os.statvfs(str(sidecar)).f_flag & os.ST_RDONLY),
                        "FOUR_SOURCE_SIDECAR_NOT_KERNEL_RO")
    require(output.is_dir() and not output.is_symlink()
            and not bool(os.statvfs(str(output)).f_flag & os.ST_RDONLY), "UNSAFE_OUTPUT_ROOT")
    print("consumer_input_boundary=PASS canonical_ro_sources=4 feature_phone_canonical_mount=false",
          file=sys.stderr)


def write_new_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def combine(manifest_path, doc, fp_record, registry):
    """All input sets are private until the owner checks producer exit."""
    require({x["clank_id"] for x in doc["lanes"]} == set(SOURCES), "FOUR_MANIFEST_DRIFT")
    require(set(registry) == set(SOURCES) | {FP_ID, "extend_builtin"}
            and registry.get("extend_builtin") is False, "REGISTRY_SET_DRIFT")
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
    previous = manifest_path.with_name("four-source-manifest.json")
    require(not previous.exists() and not previous.is_symlink(), "FOUR_EVIDENCE_EXISTS")
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
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args(argv)
    try:
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id), "INVALID_IMAGE_ID")
        runtime_boundary(args.output_root)
        spec, receipt = read_ro_json(args.spec), read_ro_json(args.receipt)
        context, registry = read_ro_json(args.request_context), read_ro_json(args.registry)
        require(set(context) == {"request_id", "request_started_at", "prior_attempt_id",
                                 "receipt_sha256"}, "REQUEST_CONTEXT_SHAPE")
        require(digest(args.receipt) == context["receipt_sha256"], "REQUEST_RECEIPT_HASH_MISMATCH")
        validate_four_spec(spec, args.image_id)
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
        result = combine(manifest, doc, fp_record, registry)
        print(json.dumps({"status": "FIVE_CHILD_MANIFEST_READY", "manifest": str(manifest),
                          "manifest_sha256": digest(manifest),
                          "registry": str(manifest.with_name("adapter-registry.json")),
                          "registry_sha256": digest(manifest.with_name("adapter-registry.json")),
                          "feature_phone_refresh_outcome": fp_record["refresh_outcome"],
                          "feature_phone_execution_freshness": fp_record["child_execution_freshness"],
                          "lanes": len(result["lanes"])}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # No path, raw SQL, source content or untrusted exception text emitted.
        code = str(exc) if isinstance(exc, IntakeError) else "CONSUMER_INPUT_CONTRACT_REJECTED"
        print("feature_phone_consumer_error=" + code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
