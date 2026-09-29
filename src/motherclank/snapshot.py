"""Snapshot construction: UNKNOWN-honest observation of each onboarded Clank.

Every adapter call is isolated; a failing Clank yields a FAILED_ADAPTER block
and never aborts the fleet snapshot. Nothing here upgrades absence to health
or zero — nulls and "UNKNOWN" pass through verbatim.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import SNAPSHOT_SCHEMA_VERSION
from . import snapshot_manifest as manifest_v1

_UNKNOWN = "UNKNOWN"


def _iso(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "model_dump"):  # pydantic v2 -> plain JSON-safe dict
        return value.model_dump(mode="json")
    return value


def _deep(value: Any) -> Any:
    """Recursively convert pydantic models/datetimes to JSON-safe structures."""
    if isinstance(value, dict):
        return {k: _deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep(v) for v in value]
    return _iso(value)


def observe_clank(adapter: Any) -> dict[str, Any]:
    """Run the read-only adapter surface for one Clank. Never raises.

    Contract v0.2: the observer surface is validated FIRST. A non-
    conforming adapter yields an isolated UNKNOWN block carrying its
    contract_violations - sibling lanes are never touched."""
    block: dict[str, Any] = {}
    from . import contract as obs_contract

    violations: list[str] = []
    if callable(getattr(adapter, "identity", None)):
        try:
            desc = adapter.identity()
            rtv = str(getattr(desc, "contract_version", "") or "") or None
            violations.extend(
                obs_contract.validate_surface(
                    adapter, runtime_contract_version=rtv))
        except Exception as exc:  # noqa: BLE001 - identity failure is itself
            # an isolation case; surface validation still runs without it.
            violations.extend(obs_contract.validate_surface(adapter))
            violations.append(f"identity() raised: {type(exc).__name__}: {exc}")
    else:
        violations.extend(obs_contract.validate_surface(adapter))
    if violations:
        return {"observation": "FAILED_ADAPTER",
                "error": "observer surface contract violation",
                "contract_violations": sorted(violations)}
    try:
        desc = adapter.identity()
        block["clank_version"] = getattr(desc, "clank_version", _UNKNOWN)
        caps = adapter.capabilities()
        block["capabilities"] = {
            "delivery_accounting": bool(getattr(caps, "supports_delivery_accounting", False)),
            "telemetry": bool(getattr(caps, "supports_telemetry", False)),
            "health": bool(getattr(caps, "supports_health", False)),
        }
    except Exception as exc:  # isolation boundary
        return {"observation": "FAILED_ADAPTER", "error": f"{type(exc).__name__}: {exc}"}

    for name in ("status", "health", "last_run"):
        try:
            value = getattr(adapter, name)()
            block[name] = _deep(value)
        except Exception as exc:
            block[name] = {"observation": "FAILED_ADAPTER", "error": f"{type(exc).__name__}: {exc}"}

    # Contract v0.3.1: optional extension dispatch is REGISTRY-DRIVEN.
    # Extensions are declared via ``contract.register_optional_extension``
    # and invoked in sorted order for deterministic output. Undeclared
    # adapter methods are NEVER invoked; a raising or malformed extension
    # is isolated to its own key without poisoning sibling extensions.
    from .contract import optional_extension_names
    for extra in sorted(optional_extension_names()):
        if not hasattr(adapter, extra):
            continue
        try:
            value = _deep(getattr(adapter, extra)())
        except Exception as exc:
            block[extra] = {"observation": "FAILED_ADAPTER", "error": f"{type(exc).__name__}: {exc}"}
            continue
        block[extra] = value
        # Capability vocabulary is part of the required v0.2 surface. A
        # malformed statement cannot be allowed to accompany a HEALTHY
        # claim; preserve the violation and isolate this evidence surface.
        if extra == "capability_states":
            try:
                from clank_runtime.contracts.capabilities import \
                    validate_capability_states
                violations = validate_capability_states(value)
                if violations:
                    block["capability_states_violations"] = violations
                    block[extra] = {
                        "observation": "FAILED_ADAPTER",
                        "error": "invalid capability state vocabulary",
                        "contract_violations": violations,
                    }
            except ImportError:
                pass  # contract module unavailable in this plane; skip check
    return block


def source_rollup(health_block: Any) -> dict[str, Any]:
    """Count sources by mapped status WITHOUT upgrading UNKNOWN to anything.
    No recorded sources is itself UNKNOWN — never a healthy zero."""
    keys = ("ok", "degraded", "failed", "blocked_zero")
    if not isinstance(health_block, dict):
        return {"unsupported": True}
    entries = health_block.get("sources") or []
    if not entries:
        out = {k: None for k in keys}
        out.update({"unknown": None, "unsupported": False, "no_sources_recorded": True})
        return out
    rollup: dict[str, Any] = {k: 0 for k in keys}
    rollup.update({"unknown": 0, "unsupported": False})
    for entry in entries:
        raw = entry.get("status") if isinstance(entry, dict) else getattr(entry, "status", _UNKNOWN)
        status = str(raw).split(".")[-1].lower()
        key = status if status in keys else "unknown"
        rollup[key] += 1
    return rollup


def db_readonly_proof(paths: list[Path]) -> dict[str, int]:
    """Direct evidence of zero mutations: sqlite total_changes per opened DB."""
    proof = {}
    for pth in paths:
        if not pth.exists():
            continue
        con = sqlite3.connect(f"file:{pth.resolve().as_posix()}?mode=ro", uri=True)
        proof[pth.name] = con.total_changes
        con.close()
    return proof


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def find_previous_snapshot_hash(snapshot_dir: Path) -> str | None:
    latest_hash = None
    latest_key = ""
    if snapshot_dir.exists():
        for file in sorted(snapshot_dir.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(rec.get("harvested_at_utc", ""))
                if key >= latest_key and rec.get("content_hash"):
                    latest_key, latest_hash = key, rec["content_hash"]
    return latest_hash


def build_snapshot(
    *,
    inventory_path: Path,
    adapters_result: dict[str, Any],
    real_state_dir: Path,
    out_dir: Path,
    continuity_events: list[dict[str, Any]] | None = None,
    snapshot_manifest_path: Path | None = None,
    expected_adapter_package_sha: str | None = None,
    expected_adapter_artifact_sha256: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return (snapshot_payload, warnings).

    Supplying a v1 manifest opts into fail-closed NAS snapshot intake. The
    legacy path remains available for historical replay but is not a claim
    of ADR-0016 snapshot conformance.
    """
    warnings: list[str] = []
    clanks_out: dict[str, Any] = {}
    ro_paths: list[Path] = []
    manifest = (manifest_v1.load_manifest(snapshot_manifest_path)
                if snapshot_manifest_path is not None else None)
    if manifest is not None:
        manifest_v1.verify_adapter_identity(
            manifest, expected_adapter_package_sha,
            expected_adapter_artifact_sha256)
    manifest_lanes = ({row["clank_id"]: row for row in manifest["lanes"]}
                      if manifest is not None else {})
    if manifest is not None:
        unknown = sorted(set(manifest_lanes) - set(adapters_result["adapters"]))
        if unknown:
            raise manifest_v1.SnapshotManifestError(
                f"manifest clank IDs absent from adapter registry: {unknown}")
        # The producer's clank_id is not enough to identify a canonical
        # child. Bind every attempted record to an independently configured
        # registry instance/lane before invoking any adapter. A historical
        # registry without this binding remains valid only for legacy intake.
        expected = adapters_result.get("expected_identities") or {}
        for row in manifest["lanes"]:
            clank_id = row["clank_id"]
            identity = expected.get(clank_id)
            if not isinstance(identity, dict):
                raise manifest_v1.SnapshotManifestError(
                    f"{clank_id}: adapter registry has no expected "
                    "instance_id/lane_id for snapshot-v1 intake")
            for field in ("instance_id", "lane_id"):
                if row[field] != identity.get(field):
                    raise manifest_v1.SnapshotManifestError(
                        f"{clank_id}: manifest {field} {row[field]!r} does not "
                        f"match registry identity {identity.get(field)!r}")

    for clank_id in sorted(adapters_result["adapters"]):
        adapter = adapters_result["adapters"][clank_id]
        if manifest is None:
            ro_paths.append(Path(adapter.db_path))
            clanks_out[clank_id] = observe_clank(adapter)
            continue
        row = manifest_lanes.get(clank_id)
        if row is None:
            clanks_out[clank_id] = {
                "observation": "SNAPSHOT_UNAVAILABLE",
                "error_code": "NO_MANIFEST_RECORD",
                "snapshot_provenance": {
                    "snapshot_contract_version": manifest_v1.SNAPSHOT_CONTRACT_VERSION,
                    "clank_id": clank_id,
                    "refresh_outcome": "UNAVAILABLE",
                    "freshness_state": "UNAVAILABLE",
                    "child_execution_freshness": "UNKNOWN",
                    "error_code": "NO_MANIFEST_RECORD",
                    "observed_at": manifest["observed_at"],
                },
            }
            continue
        provenance = manifest_v1.lineage(row)
        if row["refresh_outcome"] != "SUCCESS":
            clanks_out[clank_id] = {
                "observation": ("SNAPSHOT_REFRESH_FAILED"
                                if row["refresh_outcome"] == "FAILED"
                                else "SNAPSHOT_UNAVAILABLE"),
                "error_code": row["error_code"],
                "snapshot_provenance": provenance,
            }
            continue
        try:
            manifest_v1.verify_copy(row, Path(adapter.db_path))
        except manifest_v1.SnapshotManifestError as exc:
            clanks_out[clank_id] = {
                "observation": "SNAPSHOT_REJECTED",
                "error_code": "SNAPSHOT_VERIFICATION_FAILED",
                "error": str(exc),
                "snapshot_provenance": provenance,
            }
            warnings.append(f"{clank_id}: snapshot verification failed: {exc}")
            continue
        ro_paths.append(Path(adapter.db_path))
        if row["freshness_state"] != "FRESH":
            clanks_out[clank_id] = {
                "observation": "SNAPSHOT_STALE",
                "snapshot_provenance": provenance,
            }
            continue
        block = observe_clank(adapter)
        block["snapshot_provenance"] = provenance
        actual_schema = block.get("schema_revision")
        if (row["schema_version"] is not None
                and isinstance(actual_schema, (str, int))
                and str(actual_schema) != str(row["schema_version"])):
            block["observation"] = "SNAPSHOT_SCHEMA_MISMATCH"
            block["error_code"] = "ADAPTER_SCHEMA_MISMATCH"
        if row["child_execution_freshness"] != "FRESH" and "observation" not in block:
            block["observation"] = ("CHILD_EXECUTION_STALE"
                                    if row["child_execution_freshness"] == "STALE"
                                    else "CHILD_EXECUTION_UNKNOWN")
        clanks_out[clank_id] = block

    # F6: annotate each block with the continuity context in force at harvest
    # time (derive-time only; the registry itself stays append-only evidence).
    if continuity_events:
        from . import continuity as cont
        harvested_at = datetime.now(UTC).isoformat(timespec="seconds")
        for cid, block in clanks_out.items():
            block["continuity"] = cont.continuity_context(continuity_events, cid,
                                                          harvested_at)

    inv_text = inventory_path.read_text()
    inv_rev = _inventory_revision(inventory_path, inv_text)
    payload: dict[str, Any] = {}
    payload_extra: dict[str, Any] = {}
    ledger = _inventory_ledger(inv_text)
    if ledger:
        payload_extra["inventory_ledger"] = ledger
    payload.update({
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": "DERIVED — synthesized by Motherclank M0; Clank DBs remain authoritative",
        "harvested_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "inventory_revision": inv_rev,
        "adapter_contract_versions": adapters_result["versions"],
        "previous_snapshot_hash": find_previous_snapshot_hash(out_dir / "snapshots"),
        "read_only_proof_total_changes": db_readonly_proof(ro_paths),
        "clanks": clanks_out,
    })
    if manifest is not None:
        payload["snapshot_contract_version"] = manifest_v1.SNAPSHOT_CONTRACT_VERSION
        payload["observer_contract_version"] = manifest_v1.OBSERVER_CONTRACT_VERSION
        payload["snapshot_manifest_sha256"] = manifest["_verified_manifest_sha256"]
        payload["snapshot_manifest_observed_at"] = manifest["observed_at"]
    if continuity_events:
        from . import continuity as cont
        payload["continuity_registry_hash"] = cont.registry_hash(continuity_events)
    payload.update(payload_extra)
    payload["content_hash"] = content_hash(payload)
    for cid, block in clanks_out.items():
        failed_parts = []
        if block.get("observation") == "FAILED_ADAPTER":
            failed_parts.append("identity")
        for key, val in block.items():
            if isinstance(val, dict) and val.get("observation") == "FAILED_ADAPTER":
                failed_parts.append(key)
        if failed_parts:
            warnings.append(f"{cid}: adapter failure(s) in {', '.join(failed_parts)}")
    return payload, warnings


def _inventory_revision(inventory_path: Path, text: str) -> str:
    """Git SHA of the inventory's repository when available, else content hash.
    Pure file reads only."""
    git_dir = inventory_path.resolve().parents
    for ancestor in git_dir:
        head = ancestor / ".git" / "HEAD"
        if head.exists():
            ref = head.read_text().strip()
            if ref.startswith("ref: "):
                ref_file = ancestor / ".git" / ref[5:]
                if ref_file.exists():
                    return f"git:{ref_file.read_text().strip()}"
            return f"git:{ref}"
    return "UNVERSIONED:" + hashlib.sha256(text.encode()).hexdigest()[:12]


def append_snapshot(out_dir: Path, payload: dict[str, Any]) -> Path:
    day = payload["harvested_at_utc"][:10]
    snap_dir = out_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, default=str)
    target = snap_dir / f"{day}.jsonl"
    with target.open("a") as fh:
        fh.write(line + "\n")
    return target


def _inventory_ledger(inv_text: str) -> dict[str, str]:
    """Per-repository deployed_commit_sha from fleet.yaml content (best effort).
    Empty dict when yaml is unavailable or the structure differs — never guesses."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return {}
    try:
        doc = yaml.safe_load(inv_text) or {}
        out: dict[str, str] = {}
        for row in doc.get("deployments") or []:
            if not isinstance(row, dict):
                continue
            repo = row.get("repository")
            sha = row.get("deployed_commit_sha")
            if repo and isinstance(sha, str) and len(sha) == 40 and sha != "UNKNOWN":
                out[str(repo)] = sha
        return out
    except Exception:
        return {}
