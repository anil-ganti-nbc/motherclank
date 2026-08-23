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
    """Run the read-only adapter surface for one Clank. Never raises."""
    block: dict[str, Any] = {}
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

    for extra in ("event_summary", "delivery_summary", "qc_summary",
                  "source_lifecycle", "timeline_taxonomy", "schema_revision",
                  "current_epoch"):
        if not hasattr(adapter, extra):
            continue
        try:
            block[extra] = _deep(getattr(adapter, extra)())
        except Exception as exc:
            block[extra] = {"observation": "FAILED_ADAPTER", "error": f"{type(exc).__name__}: {exc}"}
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
) -> tuple[dict[str, Any], list[str]]:
    """Return (snapshot_payload_without_content_hash, warnings)."""
    warnings: list[str] = []
    clanks_out: dict[str, Any] = {}
    ro_paths: list[Path] = []

    for clank_id in sorted(adapters_result["adapters"]):
        adapter = adapters_result["adapters"][clank_id]
        ro_paths.append(Path(adapter.db_path))
        clanks_out[clank_id] = observe_clank(adapter)

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
