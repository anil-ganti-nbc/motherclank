"""Observer Adapter Surface Contract — spec version 0.2.

Terminology reconciliation:

- clank_runtime's ``ADAPTER_CONTRACT_VERSION`` ("0.1.0-v3") versions the
  payload/descriptor SHARED TYPES.
- THIS module versions the OBSERVER SURFACE: which methods a read-only
  fleet adapter must expose, which are optional extensions discovered
  dynamically, and how violations fail safely.

History: spec 0.1 was the implicit de-facto surface every Phase-2C-onward
adapter already implemented. 0.2 makes it explicit and mechanically
enforced at harvest time. Nothing was normalized away to get here - the
required core is exactly what all seven real adapters share.

REQUIRED CORE (every adapter; absence = contract violation):

    identity()       -> AdapterDescriptor   (who is this evidence about)
    capabilities()   -> AdapterCapabilities (honest support claims)
    status()         -> AdapterStatus       (native operational evidence)
    health()         -> HealthPayload       (per-source/collector evidence)
    last_run()       -> dict with 'supported' flag or run fields
    capability_states() -> canonical CapabilityState statements

OPTIONAL EXTENSIONS (discovered via hasattr; consumed generically when
present, ignored otherwise): event_summary, delivery_summary, qc_summary,
source_lifecycle, timeline_taxonomy, schema_revision, current_epoch,
execution_evidence, generation_summary, recent_runs, store_inventory, ...

Fail-safe rules (never crash the fleet harvest):
- missing method / raising method / bad contract major / rogue capability
  value -> that Clank's block becomes FAILED_ADAPTER-style UNKNOWN with
  machine-readable ``contract_violations``; sibling lanes unaffected;
- duplicate store identity across registry entries -> registry load error
  (fail loudly BEFORE any observation);
- everything remains read-only toward participants.
"""
from __future__ import annotations

from typing import Any

OBSERVER_SURFACE_SPEC_VERSION = "0.2"

#: Runtime descriptor-contract majors this observer understands. A newer
#: participant descriptor major means shapes we cannot yet parse honestly:
#: fail-safe to UNKNOWN rather than guess. Current fleet constant is
#: ADAPTER_CONTRACT_VERSION = "0.1.0-v3" -> major "0".
SUPPORTED_RUNTIME_CONTRACT_MAJORS = frozenset({"0"})

REQUIRED_METHODS = (
    "identity",
    "capabilities",
    "status",
    "health",
    "last_run",
    "capability_states",
)


def _runtime_major(version: Any) -> str | None:
    if not isinstance(version, str) or not version.strip():
        return None
    return str(version).split(".")[0]


def validate_surface(adapter: Any,
                     runtime_contract_version: str | None = None) -> list[str]:
    """Return machine-readable contract violations for one adapter instance.

    Empty list = conforming surface. Violations are fail-safe inputs:
    callers convert them into isolated UNKNOWN blocks, never exceptions
    that could poison sibling lanes.
    """
    violations: list[str] = []
    for method in REQUIRED_METHODS:
        if not callable(getattr(adapter, method, None)):
            violations.append(f"missing required method: {method}()")
    if runtime_contract_version is not None:
        major = _runtime_major(runtime_contract_version)
        if major is None:
            violations.append(
                f"unparseable runtime contract version: "
                f"{runtime_contract_version!r}")
        elif major not in SUPPORTED_RUNTIME_CONTRACT_MAJORS:
            violations.append(
                f"unsupported runtime contract major: {major!r} "
                f"(supported: {sorted(SUPPORTED_RUNTIME_CONTRACT_MAJORS)})")
    return violations


def surface_report(adapter: Any) -> dict[str, Any]:
    """Introspection helper for the adapter-surface audit matrix: which
    required methods exist, plus the optional extension names discovered
    on the instance."""
    optional = [name for name in (
        "event_summary", "delivery_summary", "qc_summary", "qc_records",
        "qc_summary", "source_lifecycle", "timeline_taxonomy",
        "schema_revision", "current_epoch", "execution_evidence",
        "generation_summary", "recent_runs", "store_inventory",
        "eligible_count", "telemetry", "source_summary")
        if callable(getattr(adapter, name, None))]
    return {
        "spec_version": OBSERVER_SURFACE_SPEC_VERSION,
        "required_present": sorted(
            m for m in REQUIRED_METHODS
            if callable(getattr(adapter, m, None))),
        "optional_extensions": sorted(optional),
    }
