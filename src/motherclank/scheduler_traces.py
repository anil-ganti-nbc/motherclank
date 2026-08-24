"""Motherclank P-4 — scheduler-fire trace evidence (ADR-0008 implementation).

Closes the last honest UNKNOWN: until now SCHEDULER_FIRED was UNKNOWN for
every lane because no scheduler-side invocation evidence reached the
observer plane.

Contract: a SCHEDULER TRACE is an append-only, content-hashed record of one
positively observed scheduler invocation (or one positively observed
non-fire), authored by a Diagnostic-Clank read-only host probe or equivalent
operator tooling. Motherclank never touches schedulers; it consumes traces.

Scheduler-neutral by construction: cron jobs, system/user systemd timers,
wrapper-generated invocation markers, journal/syslog lines, and manual-run
attestations all map onto ONE canonical record:

  - invoked_at present            -> SCHEDULER_FIRED = YES for that instant
  - process_started true/false    -> PROCESS_STARTED YES/NO (positive)
  - process_started null          -> PROCESS_STARTED UNKNOWN
  - no trace covering a window    -> SCHEDULER_FIRED stays UNKNOWN
                                    (absence of traces is NOT a non-fire)

The August-22 specimen becomes provable: a trace with invoked_at inside the
expected window and process_started=false is positive contrary evidence for
PROCESS_STARTED=NO -> MATERIALIZATION_GAP with pre-exec failure evidence,
distinct from any application-level outcome.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .continuity import _parse

TRACE_SCHEMA_VERSION = 1

SCHEDULER_TYPES = (
    "cron",
    "systemd_system",
    "systemd_user",
    "manual",
    "retired",
    "other",
)

EVIDENCE_SOURCES = (
    "journal",
    "syslog",
    "timer-lasttrigger",
    "wrapper-marker",
    "operator-attestation",
    "other",
)

# P-4.1: positive application-execution outcomes the probe plane may attest
# to. "no_work_due" means the application executed successfully and
# intentionally had nothing to do (due-gating/min-interval) - the OEM Radar
# live shape. Absence of this field is UNKNOWN, never assumed success.
EXECUTION_RESULTS = ("completed", "no_work_due", "failed")

REQUIRED_FIELDS = ("trace_id", "clank_id", "scheduler_type", "observed_at")


def content_hash(record: dict[str, Any]) -> str:
    canonical = {k: v for k, v in record.items() if k != "content_hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def validate_trace(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required field: {field}")
    if record.get("scheduler_type") not in SCHEDULER_TYPES:
        errors.append(f"invalid scheduler_type: {record.get('scheduler_type')!r}")
    src = record.get("evidence_source")
    if src is not None and src not in EVIDENCE_SOURCES:
        errors.append(f"invalid evidence_source: {src!r}")
    ps = record.get("process_started")
    if ps is not None and not isinstance(ps, bool):
        errors.append("process_started must be boolean or null")
    er = record.get("execution_result")
    if er is not None and er not in EXECUTION_RESULTS:
        errors.append(f"execution_result must be null or one of "
                      f"{EXECUTION_RESULTS}: {er!r}")
    ext = record.get("extractor")
    if ext is not None:
        if not isinstance(ext, dict) or not isinstance(ext.get("id"), str) \
                or not isinstance(ext.get("version"), (int, str)):
            errors.append("extractor must be {id: str, version: int|str} "
                          "or null")
    if _parse(record.get("observed_at")) is None:
        errors.append("observed_at is not an ISO timestamp")
    inv = record.get("invoked_at")
    if inv is not None and _parse(inv) is None:
        errors.append("invoked_at is not null or an ISO timestamp")
    expected = record.get("content_hash")
    if expected is not None and expected != content_hash(record):
        errors.append("content_hash mismatch")
    return errors


def make_trace(**fields: Any) -> dict[str, Any]:
    record = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "instance_id": fields.pop("instance_id", "UNKNOWN"),
        "lane_id": fields.pop("lane_id", "UNKNOWN"),
        "unit_or_job": fields.pop("unit_or_job", "UNKNOWN"),
        "invoked_at": fields.pop("invoked_at", None),
        "process_started": fields.pop("process_started", None),
        "execution_result": fields.pop("execution_result", None),
        "execution_detail": fields.pop("execution_detail", ""),
        "extractor": fields.pop("extractor", None),
        "exit_or_result": fields.pop("exit_or_result", None),
        "evidence_source": fields.pop("evidence_source", None),
        "origin": fields.pop("origin", "probe"),
        "notes": fields.pop("notes", ""),
        **fields,
    }
    errors = validate_trace(record)
    if errors:
        raise ValueError("invalid scheduler trace: " + "; ".join(errors))
    record["content_hash"] = content_hash(record)
    return record


def invocation_key(record: dict[str, Any]) -> str | None:
    """Deterministic identity of ONE scheduler invocation (P-4.2 G8).

    Two traces sharing an invocation key describe the same logical fire -
    e.g., a probe rerun that enriches earlier evidence. None when the
    record carries no invoked_at (non-fire observations have no
    invocation identity to collide)."""
    inv = record.get("invoked_at")
    if not inv:
        return None
    raw = "|".join(str(record.get(f, "")) for f in (
        "clank_id", "instance_id", "lane_id", "scheduler_type",
        "unit_or_job", "invoked_at"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _richness(record: dict[str, Any]) -> tuple:
    """Ordering for evidence enrichment: attested outcomes beat bare
    process facts; newer discoveries beat older ones."""
    has_result = 1 if record.get("execution_result") else 0
    has_ps = 1 if record.get("process_started") is not None else 0
    return (has_result, has_ps, str(record.get("discovered_at") or ""),
            str(record.get("observed_at") or ""))


def dedup_by_invocation(records: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], list[str]]:
    """Collapse duplicate observations of the same logical fire.

    Append-only discipline: nothing on disk is rewritten. The CONSUMER view
    keeps the richest evidence per invocation and reports every superseded
    trace id as a warning, preserving the audit trail by reference.
    """
    warnings: list[str] = []
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        key = invocation_key(rec)
        if key is None:                     # non-fire observation: keep as-is
            out_key = f"nf:{rec.get('trace_id', len(best))}"
            best[out_key] = rec
            order.append(out_key)
            continue
        if key not in best:
            best[key] = rec
            order.append(key)
            continue
        incumbent = best[key]
        if _richness(rec) > _richness(incumbent):
            best[key] = rec
            warnings.append(
                f"superseded trace {incumbent.get('trace_id')} by "
                f"{rec.get('trace_id')} (richer evidence, same invocation "
                f"key)")
        else:
            warnings.append(
                f"duplicate trace {rec.get('trace_id')} ignored "
                f"(same invocation as {best[key].get('trace_id')})")
    return [best[k] for k in order], warnings


def load_traces(var_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load append-only traces from <var>/scheduler/traces.jsonl.
    Tolerant loader: malformed lines warn+skip, never abort."""
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    path = Path(var_dir) / "scheduler" / "traces.jsonl"
    if not path.exists():
        return records, warnings
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"scheduler:{lineno}: unparsable line skipped ({exc})")
            continue
        errors = validate_trace(rec)
        if errors:
            warnings.append(f"scheduler:{lineno}: invalid trace skipped "
                            f"({'; '.join(errors)})")
            continue
        records.append(rec)
    records, dup_warnings = dedup_by_invocation(records)
    warnings.extend(dup_warnings)
    return records, warnings


def latest_trace_for(traces: list[dict[str, Any]], clank_id: str,
                     *, before: str | None = None,
                     window_seconds: float | None = None) -> dict[str, Any] | None:
    """Most recent positively-invoked trace for one Clank, optionally within
    ``window_seconds`` of ``before`` (typically the snapshot harvest time)."""
    ref = _parse(before) if before else None
    best: tuple[str, dict[str, Any]] | None = None
    for t in traces:
        if t.get("clank_id") != clank_id:
            continue
        inv = t.get("invoked_at")
        if inv is None:
            continue  # non-fire observations never prove a fire
        dt = _parse(inv)
        if dt is None:
            continue
        if window_seconds is not None and ref is not None:
            age = (ref - dt).total_seconds()
            if age < 0 or age > window_seconds:
                continue
        elif ref is not None and dt > ref:
            continue  # ignore traces from the future relative to observation
        key = inv
        if best is None or key > best[0]:
            best = (key, t)
    return best[1] if best else None


def stage_evidence(trace: dict[str, Any] | None) -> dict[str, str]:
    """Map one trace onto liveness stage semantics. NO requires the trace's
    own positive contrary evidence; nothing here invents NO. A non-fire
    observation (invoked_at null) proves nothing about firing."""
    if trace is None:
        return {"SCHEDULER_FIRED": "UNKNOWN"}
    fired = "YES" if trace.get("invoked_at") else "UNKNOWN"
    out = {"SCHEDULER_FIRED": fired}
    ps = trace.get("process_started")
    if ps is True:
        out["PROCESS_STARTED"] = "YES"
    elif ps is False:
        out["PROCESS_STARTED"] = "NO"   # positive: probe saw fire, no start
    else:
        out["PROCESS_STARTED"] = "UNKNOWN"
    return out


def summarize_traces(traces: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {"total": len(traces), "with_invocation": 0}
    for t in traces:
        if t.get("invoked_at"):
            out["with_invocation"] += 1
    return out
