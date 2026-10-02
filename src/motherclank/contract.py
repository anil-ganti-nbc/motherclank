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

# ---------------------------------------------------------------------------
# Optional extension registry (v0.3.1): replaces the hardcoded invocation
# list in snapshot.py. Adapters expose these methods; the observer discovers
# and invokes them generically. Adding a new extension = calling
# ``register_optional_extension`` — never editing snapshot dispatch code.
# ---------------------------------------------------------------------------

_OPTIONAL_EXTENSIONS: dict[str, dict[str, str]] = {}


def register_optional_extension(name: str, *, since: str,
                                description: str,
                                requires: str | None = None) -> None:
    _OPTIONAL_EXTENSIONS[name] = {"since": since, "description": description}
    if requires is not None:
        _OPTIONAL_EXTENSIONS[name]["requires"] = requires


def optional_extension_names() -> tuple[str, ...]:
    return tuple(sorted(_OPTIONAL_EXTENSIONS))


def optional_extension_dispatch_names() -> tuple[str, ...]:
    # Dependencies precede their dependents, with deterministic lexical order
    # otherwise. Existing extensions keep their relative invocation order.
    ordered: list[str] = []

    def add(name: str, visiting: frozenset[str] = frozenset()) -> None:
        if name in ordered:
            return
        if name in visiting:
            raise ValueError("optional extension dependency cycle")
        dependency = _OPTIONAL_EXTENSIONS[name].get("requires")
        if dependency is not None:
            add(dependency, visiting | {name})
        ordered.append(name)

    for name in optional_extension_names():
        add(name)
    return tuple(ordered)


def optional_extension_dependency(name: str) -> str | None:
    return _OPTIONAL_EXTENSIONS[name].get("requires")


def validate_optional_extension(name: str, value: Any) -> list[str]:
    """Validate transport shape, never reinterpret child domain evidence.

    The versioned observer_evidence envelope opts a producer into native
    source/diagnostic summary transport. Older adapters with similarly named
    methods keep their existing harvest behavior until explicitly qualified.
    """
    if name == "observer_evidence":
        if not isinstance(value, dict):
            return ["observer_evidence: mapping required"]
        if value.get("payload_version") != "1.0":
            return ["observer_evidence: unsupported payload_version (expected 1.0)"]
    elif name == "source_summary":
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            return ["source_summary: list of mappings required"]
    elif name == "diagnostic_summary" and not isinstance(value, dict):
        return ["diagnostic_summary: mapping required"]
    return []


def is_declared_extension(name: str) -> bool:
    return name in _OPTIONAL_EXTENSIONS


def _seed_extensions() -> None:
    seed = [
        ("event_summary", "0.1", "event counts / taxonomy summary"),
        ("delivery_summary", "0.1", "delivery accounting substrate"),
        ("qc_summary", "0.1", "QC/review disposition summary"),
        ("qc_records", "0.2", "verbatim QC records"),
        ("source_lifecycle", "0.1", "per-source lifecycle declarations"),
        ("timeline_taxonomy", "0.2", "timeline event taxonomy"),
        ("schema_revision", "0.1", "schema version evidence"),
        ("current_epoch", "0.2", "participant epoch/continuity marker"),
        ("capability_states", "0.2", "canonical CapabilityState statements"),
        ("evidence_envelopes", "0.3.1",
         "typed EvidenceEnvelope producer (P-4.2/P-4.3 architecture "
         "substrate; formally declared, not a convenience string)"),
        ("telemetry", "0.1", "recent execution telemetry envelopes"),
        ("eligible_count", "0.2", "review-eligible item counts"),
        ("generation_summary", "0.3", "discovery/generation substrates"),
        ("execution_evidence", "0.3", "dual-plane execution evidence"),
        ("recent_runs", "0.3", "ordered recent native runs"),
        ("store_inventory", "0.3", "sqlite table inventory"),
        ("provider_collection_summary", "0.3.1",
         "provider-collection plane status"),
        ("job_runs_recent", "0.3.1",
         "application-level scheduled job runs"),
    ]
    for name, since, desc in seed:
        register_optional_extension(name, since=since, description=desc)
    register_optional_extension(
        "observer_evidence", since="0.2",
        description="versioned native observer evidence envelope (payload 1.0)")
    for name in ("source_summary", "diagnostic_summary"):
        register_optional_extension(
            name, since="0.2", requires="observer_evidence",
            description="verbatim native summary; requires qualified observer evidence 1.0")


_seed_extensions()


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
    optional = [name for name in optional_extension_names()
                if callable(getattr(adapter, name, None))
                and (optional_extension_dependency(name) is None
                     or callable(getattr(adapter, optional_extension_dependency(name), None)))]
    return {
        "spec_version": OBSERVER_SURFACE_SPEC_VERSION,
        "required_present": sorted(
            m for m in REQUIRED_METHODS
            if callable(getattr(adapter, m, None))),
        "optional_extensions": sorted(optional),
    }
