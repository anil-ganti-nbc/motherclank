"""Motherclank M0 — read-only fleet harvester (ADR-0002).

Boundary invariants, enforced by tests/test_m0.py:
- consumes the Diagnostic-Clank-owned adapters UNCHANGED via sibling checkouts
- opens every Clank database strictly read-only (sqlite mode=ro)
- never writes to any Clank store; emits only its own snapshot/report files
- preserves UNKNOWN verbatim; missing data is never upgraded to healthy/zero
- one broken adapter cannot abort the fleet snapshot

This package must never import network, notification or DB-mutation machinery.
"""

__version__ = "0.1.0"

SNAPSHOT_SCHEMA_VERSION = 1
# Documentation-only mirror of the effective adapter registry (see
# adapters.BUILTIN_REGISTRY / MOTHERCLANK_ADAPTER_REGISTRY). The registry is
# the single source of truth for membership; this tuple exists so humans
# grepping the package find the current observer set.
ONBOARDED = ("watch-clank", "smartphone-clank", "korean-tech-wire",
             "feature-phone-clank", "smartwatch-clank", "oem-radar",
             "free-game-tracker", "chinese-tech-wire",
             "semiconductor-intelligence", "tablet-clank")
