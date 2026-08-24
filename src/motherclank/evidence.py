"""P-4.3 — typed EvidenceEnvelope model + consumer registry.

Motherclank's scaling law before v0.3: every genuinely new participant
evidence primitive required scattered core edits. The fix is a participant-
neutral envelope plus registered consumers:

    adapter/probe produces EvidenceEnvelope
        ↓ validation + compatibility classification (this module)
    registered consumer -> normalized derived claims
        ↓ existing synthesis/anomaly/recommendation planes

Compatibility classes:
    KNOWN                  - type registered, major supported, payload valid
    KNOWN_PAYLOAD_INVALID  - type+major known but payload violates its schema
    UNSUPPORTED_MAJOR      - type known, payload major too new (additive-minor
                             envelopes classify as KNOWN)
    UNKNOWN_TYPE           - never seen; visible/auditable, zero derived claims
    MALFORMED              - envelope itself broken (missing identity/time)

UNKNOWN never becomes a known claim. Historical envelopes remain readable:
classification is pure derivation over immutable records.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

EVIDENCE_SPEC_VERSION = "1"

_REQUIRED_FIELDS = ("evidence_type", "evidence_version", "subject",
                    "observed_at", "substrate", "payload")

#: Registered evidence types: type -> {"majors": {int}, "validate": fn}.
#: Seeded with the canonical registries that already exist; new types join
#: via register_type() WITHOUT touching synthesis/anomalies/recommendations.
_TYPES: dict[str, dict[str, Any]] = {}

_CONSUMERS: dict[str, Callable[[dict], Any]] = {}


def content_hash(envelope: dict[str, Any]) -> str:
    canonical = {k: v for k, v in envelope.items() if k != "content_hash"}
    blob = json_dumps(canonical)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _parse(value: Any) -> Any:
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def validate_envelope_shape(env: dict[str, Any]) -> list[str]:
    """Structural violations of the envelope itself (any type)."""
    errors: list[str] = []
    if not isinstance(env, dict):
        return ["envelope must be a mapping"]
    for f in _REQUIRED_FIELDS:
        if f not in env or env[f] in (None, "", {}, []):
            errors.append(f"missing/empty required field: {f}")
    if _parse(env.get("observed_at")) is None:
        errors.append("observed_at is not an ISO timestamp")
    occ = env.get("occurred_at")
    if occ is not None and _parse(occ) is None:
        errors.append("occurred_at is not null or an ISO timestamp")
    subj = env.get("subject")
    if not isinstance(subj, dict) or not subj.get("clank_id"):
        errors.append("subject.clank_id required")
    prov = env.get("provenance")
    if not isinstance(prov, dict) or not prov:
        errors.append("provenance required (non-empty mapping)")
    return errors


def make_envelope(*, evidence_type: str, evidence_version: int,
                  subject: dict[str, Any], observed_at: str,
                  substrate: str, payload: dict[str, Any],
                  provenance: dict[str, Any],
                  occurred_at: str | None = None,
                  **extra: Any) -> dict[str, Any]:
    env = {
        "evidence_spec": EVIDENCE_SPEC_VERSION,
        "evidence_type": evidence_type,
        "evidence_version": int(evidence_version),
        "subject": subject,
        "observed_at": observed_at,
        "occurred_at": occurred_at,
        "substrate": substrate,
        "payload": payload,
        "provenance": provenance,
        **extra,
    }
    env["content_hash"] = content_hash(env)
    return env


def register_type(name: str, *, majors: set[int],
                  validate_payload: Callable[[dict], list[str]]) -> None:
    """Declare an evidence TYPE: supported payload majors + its validator."""
    _TYPES[name] = {"majors": set(majors),
                    "validate": validate_payload}


def register_consumer(name: str,
                      consumer: Callable[[dict], Any]) -> None:
    """Register the derivation semantics for one evidence type. Consumers
    turn a validated envelope into normalized derived claims; Motherclank
    core never learns participant names through this interface."""
    _CONSUMERS[name] = consumer


def known_types() -> tuple[str, ...]:
    return tuple(sorted(_TYPES))


def classify(env: dict[str, Any]) -> tuple[str, list[str]]:
    """(compatibility_class, violations). Pure derivation."""
    structural = validate_envelope_shape(env)
    if structural:
        return "MALFORMED", structural
    etype = env["evidence_type"]
    spec = _TYPES.get(etype)
    if spec is None:
        return "UNKNOWN_TYPE", []
    major = int(env.get("evidence_version", -1))
    if major not in spec["majors"]:
        return "UNSUPPORTED_MAJOR", [
            f"{etype}: unsupported major {major} "
            f"(supported: {sorted(spec['majors'])})"]
    violations = spec["validate"](env.get("payload") or {})
    if violations:
        return "KNOWN_PAYLOAD_INVALID", violations
    return "KNOWN", []


# -- seeded canonical types (validators reuse the existing registries) -----

def _seed_canonical_types() -> None:
    from . import continuity as cont
    from . import scheduler_traces as straces
    from . import survivability as surv

    register_type("scheduler_trace", majors={1},
                  validate_payload=straces.validate_trace)
    register_type("continuity_event", majors={1},
                  validate_payload=cont.validate_event)
    register_type("survivability_event", majors={1},
                  validate_payload=surv.validate_record)


_seed_canonical_types()


# -- consumers ---------------------------------------------------------------

def register_consumer_for_type(name: str,
                               consumer: Callable[[dict], Any]) -> None:
    _CONSUMERS[name] = consumer


def consume_all(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify every envelope and run consumers for KNOWN ones.

    Output contract:
      derived_claims  - only from KNOWN envelopes with a registered consumer
      unknown_evidence - auditable listing of everything that produced NO
                         derived claim (unknown type / bad major / invalid /
                         malformed), with reasons
      Never raises; never invents claims from UNKNOWN.
    """
    derived: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for env in envelopes:
        cls, violations = classify(env)
        if cls == "KNOWN":
            consumer = _CONSUMERS.get(env["evidence_type"])
            if consumer is None:
                unknown.append({"content_hash": env.get("content_hash"),
                                "type": env["evidence_type"],
                                "reason": "known type, no consumer registered"})
                continue
            try:
                claims = consumer(env)
            except Exception as exc:  # noqa: BLE001 - isolation by design
                unknown.append({"content_hash": env.get("content_hash"),
                                "type": env["evidence_type"],
                                "reason": f"consumer raised: "
                                          f"{type(exc).__name__}: {exc}"})
                continue
            derived.append({"content_hash": env.get("content_hash"),
                            "type": env["evidence_type"],
                            "subject": env.get("subject"),
                            "observed_at": env.get("observed_at"),
                            "claims": claims})
        else:
            unknown.append({"content_hash": env.get("content_hash"),
                            "type": (env or {}).get("evidence_type",
                                                    "UNPARSEABLE"),
                            "reason": cls +
                                      (f": {'; '.join(violations)}"
                                       if violations else "")})
    return {"derived_claims": derived, "unknown_evidence": unknown,
            "count": len(envelopes)}
