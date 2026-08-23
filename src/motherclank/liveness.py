"""Motherclank F6b — execution-liveness model (ADR-0008 draft).

Observer-only derivation answering a question the 2026-08-22 incident made
canonical: "expected executions exist — did runs actually materialize?"

The lesson codified here:

    SCHEDULE_EXPECTED != SCHEDULER_FIRED != PROCESS_STARTED
        != RUN_MATERIALIZED != RUN_COMPLETED != OUTCOME_RECORDED

A failed shell redirect BEFORE collector start produces scheduler evidence
and NOTHING else. That is a MATERIALIZATION_GAP — an execution-plane fact —
and must never be diagnosed as collector regression (application logic never
ran; it cannot have failed).

Honesty rules:

- Every stage supports YES / NO / UNKNOWN / NOT_APPLICABLE with provenance.
- Absence of evidence is UNKNOWN, never NO.
- Intentional dormancy (Tablet-class: MANUAL/RETIRED policies) produces no
  missing-run alarm. Abandoned artifacts (stale unit files) prove nothing;
  the expectations registry — operator-owned, append-only — outranks them.
- Scheduler-neutral: cron, systemd timers, manual/on-demand, finite soaks,
  and Windows experimental lanes are all expressible via policy entries.

Expectations registry: ``<var>/liveness/execution-expectations.jsonl``
(append-only, content-hashed, tolerant loader, same discipline as the
continuity registry).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .continuity import _parse  # shared ISO parsing discipline

LIVENESS_SCHEMA_VERSION = 1

STAGES = (
    "SCHEDULE_EXPECTED",
    "SCHEDULER_FIRED",
    "PROCESS_STARTED",
    "RUN_MATERIALIZED",
    "RUN_COMPLETED",
    "OUTCOME_RECORDED",
)

EVIDENCE_VALUES = ("YES", "NO", "UNKNOWN", "NOT_APPLICABLE")

EXECUTION_POLICIES = (
    "PERIODIC",
    "FINITE_SOAK",
    "MANUAL",
    "ON_DEMAND",
    "DISABLED",
    "RETIRED",
    "UNKNOWN",
)

# Policies for which absence of runs is EXPECTED behaviour, not an anomaly.
DORMANT_POLICIES = {"MANUAL", "ON_DEMAND", "DISABLED", "RETIRED"}

# Derived liveness dimension. Orthogonal to operational health and to
# continuity_state; never collapses into either.
LIVENESS_STATES = (
    "CURRENT",                  # latest run evidence within expected cadence
    "MATERIALIZATION_GAP",      # scheduler fired recently, no run since
    "EXECUTION_STALE",          # runs older than window; cause unevidenced
    "SCHEDULER_SILENT",         # no scheduler evidence within window
    "INTENTIONALLY_DORMANT",    # policy says runs are not expected
    "UNKNOWN",
)

REQUIRED_FIELDS = (
    "expectation_id",
    "clank_id",
    "policy",
)


def content_hash(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "content_hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def validate_expectation(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required field: {field}")
    if record.get("policy") not in EXECUTION_POLICIES:
        errors.append(f"invalid policy: {record.get('policy')!r}")
    if record.get("active") not in (True, False):
        errors.append("active must be boolean")
    cadence = record.get("cadence_seconds")
    if cadence is not None and (not isinstance(cadence, (int, float)) or cadence <= 0):
        errors.append("cadence_seconds must be a positive number or null")
    expected_hash = record.get("content_hash")
    if expected_hash is not None and expected_hash != content_hash(record):
        errors.append("content_hash mismatch")
    return errors


def make_expectation(**fields: Any) -> dict[str, Any]:
    record = {
        "schema_version": LIVENESS_SCHEMA_VERSION,
        "instance_id": fields.pop("instance_id", "UNKNOWN"),
        "lane_id": fields.pop("lane_id", "UNKNOWN"),
        "authority": fields.pop("authority", "UNKNOWN"),
        "cadence_seconds": fields.pop("cadence_seconds", None),
        "active": fields.pop("active", True),
        "effective_end": fields.pop("effective_end", None),
        "notes": fields.pop("notes", ""),
        **fields,
    }
    errors = validate_expectation(record)
    if errors:
        raise ValueError("invalid execution expectation: " + "; ".join(errors))
    record["content_hash"] = content_hash(record)
    return record


def load_expectations(var_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load the append-only expectations registry. Malformed lines warn+skip."""
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    path = Path(var_dir) / "liveness" / "execution-expectations.jsonl"
    if not path.exists():
        return records, warnings
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"liveness:{lineno}: unparsable line skipped ({exc})")
            continue
        errors = validate_expectation(rec)
        if errors:
            warnings.append(f"liveness:{lineno}: invalid expectation skipped "
                            f"({'; '.join(errors)})")
            continue
        records.append(rec)
    return records, warnings


def _effective_at(record: dict[str, Any], at: str) -> bool:
    t = _parse(at)
    if t is None or record.get("active") is not True:
        return False
    end = record.get("effective_end")
    parsed_end = _parse(end) if end is not None else None
    return parsed_end is None or t < parsed_end


def expectation_for(records: list[dict[str, Any]], clank_id: str,
                    at: str) -> dict[str, Any] | None:
    """Latest active expectation for one Clank at one instant."""
    applicable = [r for r in records
                  if r.get("clank_id") == clank_id and _effective_at(r, at)]
    if not applicable:
        return None
    applicable.sort(key=lambda r: str(r.get("content_hash")))  # deterministic tie-break
    return applicable[-1]


def _latest_run_ts(block: dict[str, Any]) -> tuple[str | None, str]:
    last_run = block.get("last_run") or {}
    ts = (last_run.get("finished_at") or last_run.get("completed_at")
          or last_run.get("started_at"))
    provenance = "last_run." + next(
        (k for k in ("finished_at", "completed_at", "started_at") if last_run.get(k)),
        "?")
    return ts, provenance


def _invocation_ts(block: dict[str, Any]) -> str | None:
    pair = block.get("scheduler_pair") or {}
    inv = pair.get("last_scheduler_invocation")
    return str(inv) if inv else None


def stage_view(block: dict[str, Any],
               expectation: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Per-stage YES/NO/UNKNOWN/NOT_APPLICABLE with provenance. Silence is
    UNKNOWN; NO requires positive contrary evidence, which observer-tier
    inputs rarely provide."""
    view: dict[str, dict[str, str]] = {}
    policy = (expectation or {}).get("policy")

    def stage(name: str, value: str, prov: str) -> None:
        assert value in EVIDENCE_VALUES
        view[name] = {"value": value, "provenance": prov}

    if expectation is None:
        stage("SCHEDULE_EXPECTED", "UNKNOWN", "no active expectation registered")
        for s in STAGES[1:]:
            stage(s, "UNKNOWN", "schedule expectation unevidenced")
        return view

    sched_expected = {"PERIODIC", "FINITE_SOAK"}
    if policy in sched_expected:
        stage("SCHEDULE_EXPECTED", "YES",
              f"policy={policy} authority={(expectation or {}).get('authority', 'UNKNOWN')}")
    elif policy in DORMANT_POLICIES or policy == "UNKNOWN":
        stage("SCHEDULE_EXPECTED", "NOT_APPLICABLE", f"policy={policy}")
        for s in STAGES[1:]:
            stage(s, "NOT_APPLICABLE", f"policy={policy} does not schedule stages")
        return view

    run_ts, run_prov = _latest_run_ts(block)
    inv = _invocation_ts(block)
    last_run = block.get("last_run") or {}

    stage("SCHEDULER_FIRED",
          "YES" if inv else "UNKNOWN",
          "scheduler_pair.last_scheduler_invocation" if inv
          else "no invocation evidence visible to observer")
    stage("PROCESS_STARTED",
          "YES" if last_run.get("started_at") else "UNKNOWN",
          "last_run.started_at" if last_run.get("started_at")
          else "no process-start evidence visible to observer")
    stage("RUN_MATERIALIZED",
          "YES" if run_ts else "UNKNOWN",
          run_prov if run_ts else "no run row visible to observer")
    finished = last_run.get("finished_at") or last_run.get("completed_at")
    stage("RUN_COMPLETED", "YES" if finished else "UNKNOWN",
          "last_run.finished_at" if finished else "no completion evidence")
    outcome = last_run.get("status") or last_run.get("outcome")
    stage("OUTCOME_RECORDED", "YES" if outcome else "UNKNOWN",
          "last_run.status/outcome" if outcome else "no outcome record visible")
    return view


def derive_liveness(block: dict[str, Any], expectation: dict[str, Any] | None,
                    *, observed_at: str,
                    grace_multiplier: float = 2.0) -> dict[str, Any]:
    """Strongest justified statement about execution for one Clank block."""
    policy = (expectation or {}).get("policy")
    result: dict[str, Any] = {
        "liveness_state": "UNKNOWN",
        "policy": policy or "UNKNOWN",
        "stages": stage_view(block, expectation),
        "orthogonal_to_operational_health": True,
        "provenance": {"derived_by": "motherclank-liveness",
                       "observed_at": observed_at},
    }

    if expectation is None:
        return result
    if policy in DORMANT_POLICIES:
        result["liveness_state"] = "INTENTIONALLY_DORMANT"
        return result
    if policy == "FINITE_SOAK":
        # finite soaks are expected to stop; only staleness-with-expectation
        # would be anomalous, and soak completion belongs to the soak plane
        result["liveness_state"] = "CURRENT"
        result["notes"] = "finite-soak policy: cadence enforcement not applied"
        return result

    cadence = expectation.get("cadence_seconds")
    if cadence is None:
        return result  # cannot judge windows without a declared cadence

    window = float(cadence) * grace_multiplier
    now = _parse(observed_at)
    run_ts, run_prov = _latest_run_ts(block)
    inv = _invocation_ts(block)
    run_dt = _parse(run_ts) if run_ts else None
    inv_dt = _parse(inv) if inv else None

    def age(dt: Any) -> float | None:
        if now is None or dt is None:
            return None
        return (now - dt).total_seconds()

    run_age = age(run_dt)
    inv_age = age(inv_dt)

    if run_age is not None and run_age <= window:
        result["liveness_state"] = "CURRENT"
        result["evidence"] = {"last_run_age_seconds": run_age, "source": run_prov}
        return result

    # Positive scheduler evidence without a recent run => materialization gap
    if inv_dt is not None and (run_dt is None or inv_dt > run_dt):
        if inv_age is not None and inv_age <= window:
            result["liveness_state"] = "MATERIALIZATION_GAP"
            result["evidence"] = {
                "last_invocation_age_seconds": inv_age,
                "last_run_age_seconds": run_age,
                "interpretation": ("scheduler fired; no newer application run "
                                   "materialized; cause NOT attributed"),
            }
            # Positive contrary evidence justifies NO for the downstream
            # stages of THIS invocation: if no run materialized after the
            # latest fire, none could have completed or recorded an outcome.
            result["stages"]["PROCESS_STARTED"] = {
                "value": "NO",
                "provenance": "no process-start evidence newer than the "
                              "latest scheduler invocation"}
            result["stages"]["RUN_MATERIALIZED"] = {
                "value": "NO",
                "provenance": "scheduler evidence newer than newest run row"}
            result["stages"]["RUN_COMPLETED"] = {
                "value": "NO",
                "provenance": "implied: no run materialized for the latest "
                              "invocation"}
            result["stages"]["OUTCOME_RECORDED"] = {
                "value": "NO",
                "provenance": "implied: no run materialized for the latest "
                              "invocation"}
            return result

    if run_age is not None:
        result["liveness_state"] = "EXECUTION_STALE"
        result["evidence"] = {"last_run_age_seconds": run_age,
                              "window_seconds": window,
                              "cause": "UNKNOWN"}
        return result

    if inv_dt is None:
        # No invocation evidence AND no run evidence. Absence of evidence is
        # UNKNOWN, never SCHEDULER_SILENT — unless the block POSITIVELY
        # declares its scheduler evidence current-but-empty (observer saw
        # the plane and it showed nothing).
        pair = block.get("scheduler_pair") or {}
        positively_empty = (isinstance(pair, dict)
                            and pair.get("evidence_current") is True
                            and not inv)
        authority = expectation.get("authority", "UNKNOWN")
        result["liveness_state"] = ("SCHEDULER_SILENT" if positively_empty
                                    else "UNKNOWN")
        result["evidence"] = {"authority_declared": authority,
                              "scheduler_evidence_current": bool(
                                  isinstance(pair, dict) and pair.get("evidence_current")),
                              "note": "absence of evidence is UNKNOWN"}
    return result


def annotate_blocks(snapshot_payload: dict[str, Any],
                    expectations: list[dict[str, Any]],
                    grace_multiplier: float = 2.0) -> dict[str, Any]:
    """Derive-time annotation of snapshot blocks with liveness context.
    Returns a copy; append-only artifacts are never modified."""
    at = snapshot_payload.get("harvested_at_utc", "")
    out = dict(snapshot_payload)
    clanks = {}
    for cid, block in out.get("clanks", {}).items():
        exp = expectation_for(expectations, cid, at)
        annotated = dict(block)
        annotated["liveness"] = derive_liveness(
            block, exp, observed_at=at, grace_multiplier=grace_multiplier)
        clanks[cid] = annotated
    out["clanks"] = clanks
    return out
