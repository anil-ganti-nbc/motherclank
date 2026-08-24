"""Adapter loading: reuse Diagnostic Clank's read-only adapters UNCHANGED.

The adapters live in the diagnostic-clank checkout (clank-fleet + clank-runtime
packages; not on PyPI). We locate them as workspace siblings and import from
there - no vendoring, no copying, so adapter evolution stays single-sourced.

F2 (architecture convergence): the set of onboarded Clanks is REGISTRY-driven,
not hardcoded. A registry entry maps a clank_id to its adapter module/class
and read-only DB copy name. Onboarding a manifest-complete observer Clank
therefore requires ZERO edits to Motherclank source: supply a registry file.

Registry resolution order:
  1. explicit ``registry_path`` argument (or CLI --adapter-registry)
  2. ``MOTHERCLANK_ADAPTER_REGISTRY`` environment variable
  3. built-in default registry (the four validated Phase 2C Clanks)

A registry file may EXTEND the builtin set (merge) when it contains
``"extend_builtin": true``, or fully REPLACE it otherwise.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_REQUIRED = [
    ("clank_fleet.adapters.watch_clank", "WatchClankAdapter"),
    ("clank_fleet.adapters.smartphone_clank", "SmartphoneClankAdapter"),
    ("clank_fleet.adapters.korean_tech_wire", "KoreanTechWireAdapter"),
    ("clank_fleet.adapters.feature_phone", "FeaturePhoneAdapter"),
]

BUILTIN_REGISTRY: dict[str, dict[str, Any]] = {
    "watch-clank": {
        "module": "clank_fleet.adapters.watch_clank",
        "class": "WatchClankAdapter",
        "db": "watch_clank.db",
        "qc": True,
    },
    "smartphone-clank": {
        "module": "clank_fleet.adapters.smartphone_clank",
        "class": "SmartphoneClankAdapter",
        "db": "smartphone_clank.db",
        "qc": True,
    },
    "korean-tech-wire": {
        "module": "clank_fleet.adapters.korean_tech_wire",
        "class": "KoreanTechWireAdapter",
        "db": "korean_tech_wire.db",
        "qc": True,
    },
    "feature-phone-clank": {
        "module": "clank_fleet.adapters.feature_phone",
        "class": "FeaturePhoneAdapter",
        "db": "feature_phone_clank.db",
        "qc": False,
    },
    # P4-G6: onboarded via registry alone - zero Motherclank-core edits.
    # Adapter is at schema-introspection stage (live semantic mapping
    # BLOCKED); it reports UNKNOWN-honest blocks until the restored DB's
    # schema is mapped by someone with read access.
    "smartwatch-clank": {
        "module": "clank_fleet.adapters.smartwatch_clank",
        "class": "SmartwatchClankAdapter",
        "db": "smartwatch-clank.sqlite3",
        "qc": False,
    },
    # Hot-swap specimen lane (observer expansion phase): onboarded via this
    # data row alone. No Motherclank core module references OEM Radar.
    # db filename matches the operator-verified real-state copy produced by
    # scripts/refresh-real-state.sh (inner file is radar.db, NOT oem_radar.db
    # - resource naming is not identity).
    "oem-radar": {
        "module": "clank_fleet.adapters.oem_radar",
        "class": "OemRadarAdapter",
        "db": "radar.db",
        "qc": False,
    },
    # Observer expansion phase: onboarded via this data row alone.
    # db filename pending operator confirmation (FGT store lives in a
    # container volume; inner name not yet live-verified - never guessed,
    # per the incident record).
    "free-game-tracker": {
        "module": "clank_fleet.adapters.free_game_tracker",
        "class": "FreeGameTrackerAdapter",
        "db": "newsroom.db",  # operator-confirmed 2026-08-24; see refresh-real-state.sh
        "qc": False,
    },
}


class AdapterPlaneUnavailable(RuntimeError):
    pass


def _candidate_roots(explicit: Path | None) -> list[Path]:
    roots: list[Path] = []
    if explicit:
        roots.append(explicit)
        if (explicit / "diagnostic-clank").exists():
            roots.append(explicit / "diagnostic-clank")
    here = Path(__file__).resolve()
    for base in (*here.parents, *here.parents[0].parents):
        cand = base / "diagnostic-clank"
        if cand.exists():
            roots.append(cand)
    return roots


def ensure_adapter_plane(diagnostic_clank_path: Path | None = None) -> None:
    """Put clank-fleet/clank-runtime sources on sys.path, idempotently."""
    for root in _candidate_roots(diagnostic_clank_path):
        fleet = root / "clank-fleet" / "src"
        runtime = root / "clank-runtime" / "src"
        if (fleet / "clank_fleet" / "adapters").exists() and (runtime / "clank_runtime").exists():
            for pth in (str(fleet), str(runtime)):
                if pth not in sys.path:
                    sys.path.insert(0, pth)
            return
    raise AdapterPlaneUnavailable(
        "diagnostic-clank checkout with clank-fleet/clank-runtime sources not found; "
        "pass --adapters-src pointing at it"
    )


def load_registry(registry_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load the effective adapter registry. Malformed overrides fail loudly:
    silent partial onboarding would violate the honesty contract."""
    registry = {cid: dict(entry) for cid, entry in BUILTIN_REGISTRY.items()}
    path = registry_path or os.environ.get("MOTHERCLANK_ADAPTER_REGISTRY")
    if not path:
        return registry
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        import yaml  # optional dependency, consistent with inventory loading
        doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise AdapterPlaneUnavailable(f"adapter registry must be a mapping: {path}")
    extend = bool(doc.pop("extend_builtin", True))
    if not extend:
        registry.clear()
    seen_stores: dict[str, str] = {}
    for cid, entry in doc.items():
        if not isinstance(cid, str) or not isinstance(entry, dict):
            raise AdapterPlaneUnavailable(f"invalid registry row: {cid!r}")
        for field in ("module", "class", "db"):
            if not entry.get(field):
                raise AdapterPlaneUnavailable(
                    f"registry row {cid!r} missing required field {field!r}")
        registry[str(cid)] = {
            "module": entry["module"],
            "class": entry["class"],
            "db": entry["db"],
            "qc": bool(entry.get("qc", False)),
        }
    # Duplicate store identity (final sweep, builtin included): two lanes
    # pointing at one DB file would silently cross-contaminate evidence.
    seen_stores: dict[str, str] = {}
    for cid, entry in registry.items():
        db_key = str(entry["db"])
        if db_key in seen_stores:
            raise AdapterPlaneUnavailable(
                f"duplicate store identity: {db_key} claimed by both "
                f"{seen_stores[db_key]!r} and {cid!r}")
        seen_stores[db_key] = cid
    return registry


def build_adapters(real_state_dir: Path,
                   diagnostic_clank_path: Path | None = None,
                   registry_path: Path | str | None = None) -> dict[str, object]:
    """Instantiate every registered observer adapter against read-only DB copies."""
    ensure_adapter_plane(diagnostic_clank_path)
    registry = load_registry(registry_path)
    if not registry:
        raise AdapterPlaneUnavailable("effective adapter registry is empty")
    d = real_state_dir
    adapters: dict[str, Any] = {}
    qc_ids: list[str] = []
    versions: dict[str, Any] = {}
    from clank_runtime.version import ADAPTER_CONTRACT_VERSION  # noqa: PLC0415

    for cid in sorted(registry):
        entry = registry[cid]
        module = __import__(entry["module"], fromlist=[entry["class"]])
        cls = getattr(module, entry["class"])
        adapters[cid] = cls(db_path=d / entry["db"])
        if entry.get("qc"):
            qc_ids.append(cid)

    versions = {"adapter_contract_version": ADAPTER_CONTRACT_VERSION}
    return {"adapters": adapters, "versions": versions, "qc_adapters": qc_ids}
