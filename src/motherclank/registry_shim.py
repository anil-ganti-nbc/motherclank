"""ADR-0003 §2: inventory-derived ClankRegistry for Inbox bridging.

Motherclank owns NO fleet-membership truth. Valid Clank identities are
derived at runtime from the canonical machine-readable fleet inventory
(`fleet.yaml`, owned by diagnostic-clank per ADR-0001) — the same operator-
supplied `--inventory` file the harvest path already consumes.

Selection semantics (fleet.yaml schema v2.0):
  repositories[].classification == CLANK   → valid onboarded domain Clank.
Everything else (GOVERNANCE, SUPPORTING_SYSTEM, SUPERSEDED_CANDIDATE) is
excluded, so control-plane and governance repos are never registered.

Failure semantics: a missing/unparseable/unusable inventory raises before any
Inbox write. There is deliberately no fallback list: if membership cannot be
established from canonical data, bridging must fail loudly.

A newly onboarded or declassified Clank is reflected automatically on the
next run — no code change in Motherclank.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from clank_runtime.registry.core import ClankRegistration, ClankRegistry


class InventoryUnusableError(RuntimeError):
    """The fleet inventory could not establish valid Clank membership."""


def clank_ids_from_inventory(inventory_path: Path) -> tuple[str, ...]:
    """Return valid onboarded domain-Clank ids from canonical fleet.yaml."""
    try:
        text = inventory_path.read_text()
    except OSError as exc:
        raise InventoryUnusableError(f"inventory unreadable: {inventory_path}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InventoryUnusableError(f"inventory unparseable YAML: {inventory_path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise InventoryUnusableError(f"inventory is not a mapping: {inventory_path}")
    rows = doc.get("repositories")
    if not isinstance(rows, list) or not rows:
        raise InventoryUnusableError(
            f"inventory has no usable 'repositories' list: {inventory_path}")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise InventoryUnusableError(f"non-mapping repository row: {row!r}")
        name = row.get("name")
        classification = row.get("classification")
        if not isinstance(name, str) or not name.strip():
            raise InventoryUnusableError(f"repository row missing 'name': {row!r}")
        if classification == "CLANK":
            ids.append(name)
    if not ids:
        raise InventoryUnusableError(
            f"inventory establishes no CLANK-classified members: {inventory_path}")
    return tuple(sorted(ids))


def operator_registry(inventory_path: Path) -> ClankRegistry:
    """Build the bridging registry purely from canonical inventory data."""
    reg = ClankRegistry()
    for clank_id in clank_ids_from_inventory(inventory_path):
        reg.register(ClankRegistration(clank_id=clank_id, display_name=clank_id))
    return reg
