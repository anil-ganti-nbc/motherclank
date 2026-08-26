"""Motherclank P-FINAL — canonical fleet closeout artifact.

One machine-readable record answering "what is deployed, what proves it,
what still owes" per fleet lane, so no future archaeology exercise is
needed. This is an OBSERVER capability: every value either comes from a
typed declaration (lane configs), the ADR-0006 continuity registry,
scheduler-fire traces (P-4), or an OPERATOR-VERIFIED live-evidence file.
Anything without evidence is the literal string UNKNOWN — never guessed,
never inferred from healthy-looking neighbors.

Grounding rules (inviolable):
  * declarations describe intent; they can never manufacture observations;
  * evidence files are operator-authored facts with a required sha256;
    Motherclank validates structure and hashes, then copies verbatim;
  * scheduler attestation uses ONLY P-4 traces (fired | missed | unknown);
    absence of traces is UNKNOWN, not "not fired";
  * RESTORED_HISTORY != CONTINUOUS and NEW_EPOCH != CONTINUOUS — epochs are
    copied from the registry, never normalized;
  * participant exit-code 0 alone never yields an execution_result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CLOSEOUT_SCHEMA_VERSION = "1"

# Completion vocabulary (§4 of the completion campaign). Evidence-free
# lanes are UNKNOWN, not optimistically classified.
CLASSIFICATIONS = (
    "COMPLETE",
    "CODE_COMPLETE_LIVE_UNPROVEN",
    "LIVE_COMPLETE_BOUNDED_DEBT",
    "BLOCKING_GAP",
    "DEFERRED_NONBLOCKING",
    "UNSUPPORTED_BY_PARTICIPANT",
    "UNKNOWN",
)

REQUIRED_EVIDENCE_FIELDS = (
    "clank_id",
    "instance_id",
    "lane_id",
)


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"{Path(path).name}:{lineno}: unparsable ({exc})")
            continue
        out.append(rec)
    return out, warnings


def validate_live_evidence(records: list[dict[str, Any]]) -> list[str]:
    """Structural contract for operator evidence entries. Values are copied,
    so only identity and shape are policed here."""
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for i, rec in enumerate(records, 1):
        for f in REQUIRED_EVIDENCE_FIELDS:
            v = rec.get(f)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"evidence[{i}]: missing {f}")
        key = (rec.get("clank_id"), rec.get("instance_id"), rec.get("lane_id"))
        if key in seen:
            errors.append(f"evidence[{i}]: duplicate lane identity {key}")
        seen.add(key)
        cls = rec.get("completion_classification", "UNKNOWN")
        if cls not in CLASSIFICATIONS:
            errors.append(f"evidence[{i}]: invalid completion_classification "
                          f"{cls!r}")
    return errors


def _latest_trace_for_lane(traces: list[dict[str, Any]],
                           ev: dict[str, Any]) -> dict[str, Any] | None:
    cid = ev.get("clank_id")
    best = None
    for t in traces:
        if t.get("clank_id") != cid:
            continue
        inv = t.get("invoked_at")
        if not inv:
            continue
        # Prefer matching instance/lane when the trace declares them.
        t_inst = t.get("instance_id", "UNKNOWN")
        if t_inst not in ("UNKNOWN", None) and \
                ev.get("instance_id") not in (None, "UNKNOWN") and \
                t_inst != ev["instance_id"]:
            continue
        if best is None or str(inv) > str(best.get("invoked_at")):
            best = t
    return best


def build_closeout(*, generated_at_utc: str,
                   live_evidence: list[dict[str, Any]] | None = None,
                   lane_configs: list[dict[str, Any]] | None = None,
                   continuity_events: list[dict[str, Any]] | None = None,
                   scheduler_traces: list[dict[str, Any]] | None = None,
                   ) -> dict[str, Any]:
    """Deterministically assemble the closeout payload. Never raises on
    missing optional inputs; missingness becomes UNKNOWN fields."""

    from . import continuity as cont

    evidence = sorted(live_evidence or [],
                      key=lambda r: (r["clank_id"], r["instance_id"],
                                     r["lane_id"]))
    configs_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cfg in lane_configs or []:
        configs_by_key.setdefault(
            (cfg.get("clank_id"), cfg.get("instance_id"), cfg.get("lane_id")),
            cfg)

    lanes_out: list[dict[str, Any]] = []
    for ev in evidence:
        key = (ev["clank_id"], ev["instance_id"], ev["lane_id"])
        cfg = configs_by_key.pop(key, None)

        # Continuity: evidence block if the operator supplied one; otherwise
        # derive from the registry at the evidence's verification instant;
        # otherwise UNKNOWN. Epochs are copied, never normalized.
        cont_block = ev.get("continuity")
        if not isinstance(cont_block, dict) and continuity_events is not None:
            ctx = cont.continuity_context(
                continuity_events, ev["clank_id"],
                ev.get("verified_at_utc") or "")
            cont_block = {
                "continuity_state": ctx.get("continuity_state",
                                            "UNKNOWN_CONTINUITY"),
                "epoch_id": ctx.get("epoch_id", "UNKNOWN"),
                "active_event_ids": ctx.get("active_event_ids", []),
                "evidence_refs": [],
            }
        elif not isinstance(cont_block, dict):
            cont_block = {"continuity_state": "UNKNOWN_CONTINUITY",
                          "epoch_id": "UNKNOWN",
                          "active_event_ids": [], "evidence_refs": []}

        # Scheduler attestation strictly via P-4 traces.
        trace = _latest_trace_for_lane(scheduler_traces or [], ev)
        if trace is not None:
            attestation = {
                "SCHEDULER_FIRED": "YES" if trace.get("invoked_at")
                else "UNKNOWN",
                "PROCESS_STARTED":
                    "YES" if trace.get("process_started") is True
                    else "NO" if trace.get("process_started") is False
                    else "UNKNOWN",
                "execution_result": trace.get("execution_result"),  # null law
                "trace_id": trace.get("trace_id"),
                "invoked_at": trace.get("invoked_at"),
            }
        else:
            attestation = {"SCHEDULER_FIRED": "UNKNOWN",
                           "PROCESS_STARTED": "UNKNOWN",
                           "execution_result": None,
                           "trace_id": None, "invoked_at": None}

        lanes_out.append({
            "clank_id": ev["clank_id"],
            "instance_id": ev["instance_id"],
            "lane_id": ev["lane_id"],
            "deployment": {
                "deployed_commit_sha": ev.get("deployed_commit_sha",
                                              "UNKNOWN"),
                "environment": ev.get("environment", "UNKNOWN"),
                "host": ev.get("host", "UNKNOWN"),
                "verified_at_utc": ev.get("verified_at_utc", "UNKNOWN"),
            },
            "datastore": {
                "identity": ev.get("datastore_identity", "UNKNOWN"),
                "path_or_volume": ev.get("datastore_path", "UNKNOWN"),
                "schema_revision": ev.get("schema_revision", "UNKNOWN"),
            },
            "continuity": {
                "continuity_state":
                    cont_block.get("continuity_state", "UNKNOWN_CONTINUITY"),
                "epoch_id": cont_block.get("epoch_id", "UNKNOWN"),
                "active_event_ids": cont_block.get("active_event_ids", []),
                "evidence_refs": cont_block.get("evidence_refs", []),
            },
            "scheduler": {
                "authority": (cfg or {}).get("authority",
                                             ev.get("scheduler_authority",
                                                    "UNKNOWN")),
                "type": (cfg or {}).get("scheduler_type",
                                        ev.get("scheduler_type", "UNKNOWN")),
                "cadence_seconds": (cfg or {}).get("cadence_seconds"),
                "attestation": attestation,
            },
            "notification_capability": ev.get("notification_capability",
                                              "UNKNOWN"),
            "source_maturity_summary": ev.get("source_maturity_summary",
                                              "UNKNOWN"),
            "backup_recovery_evidence": ev.get("backup_recovery_evidence", []),
            "latest_live_validation": {
                "at_utc": ev.get("verified_at_utc", "UNKNOWN"),
                "evidence_ref": ev.get("validation_evidence_ref", "UNKNOWN"),
            },
            "blocking_debt": ev.get("blocking_debt", []),
            "bounded_debt": ev.get("bounded_debt", []),
            "rollback_surface": ev.get("rollback_surface", "UNKNOWN"),
            "evidence_stale": bool(ev.get("evidence_stale", False)),
            "completion_classification":
                ev.get("completion_classification", "UNKNOWN"),
        })

    counts: dict[str, int] = {}
    for lane in lanes_out:
        cls = lane["completion_classification"]
        counts[cls] = counts.get(cls, 0) + 1
    unknown_fields = 0
    for lane in lanes_out:
        for section in ("deployment", "datastore"):
            unknown_fields += sum(
                1 for v in lane[section].values() if v == "UNKNOWN")

    return {
        "schema_version": CLOSEOUT_SCHEMA_VERSION,
        "derived_label": ("DERIVED fleet closeout — Motherclank P-FINAL; "
                          "operator-evidence carried verbatim, "
                          "UNKNOWN preserved"),
        "generated_at_utc": generated_at_utc,
        "classification_vocabulary": list(CLASSIFICATIONS),
        "inputs": {
            "live_evidence_records": len(evidence),
            "unmatched_lane_configs": sorted(
                k for k in configs_by_key),
            "continuity_event_count": len(continuity_events or []),
            "scheduler_trace_count": len(scheduler_traces or []),
        },
        "counts": {
            "lanes": len(lanes_out),
            "by_completion_classification": counts,
            "unknown_declaration_fields": unknown_fields,
        },
        "lanes": lanes_out,
    }


def build_closeout_from_files(*, generated_at_utc: str,
                              live_evidence_path: Path,
                              lane_configs_path: Path | None = None,
                              continuity_events_path: Path | None = None,
                              scheduler_traces_path: Path | None = None,
                              ) -> tuple[dict[str, Any], list[str]]:
    """File-loading variant. Fail-closed on structural evidence errors;
    tolerant on optional ambient inputs (warnings instead)."""
    from .lane_config import load_lane_configs
    from . import scheduler_traces as straces

    warnings: list[str] = []

    ev_recs, w = _load_jsonl(live_evidence_path)
    warnings.extend(w)
    struct_errors = validate_live_evidence(ev_recs)
    if struct_errors:
        raise ValueError("invalid live evidence: " + "; ".join(struct_errors))

    configs: list[dict[str, Any]] = []
    if lane_configs_path is not None:
        configs, w = load_lane_configs(lane_configs_path)
        warnings.extend(w)

    events: list[dict[str, Any]] = []
    if continuity_events_path is not None:
        raw_events, w = _load_jsonl(continuity_events_path)
        warnings.extend(w)
        bad = [rec for rec in raw_events if cont_validate(rec)]
        for rec_bad in bad:
            warnings.append(f"continuity: invalid event skipped "
                            f"({rec_bad.get('event_id')})")
        events = [rec for rec in raw_events if not cont_validate(rec)]

    traces: list[dict[str, Any]] = []
    if scheduler_traces_path is not None:
        parent = Path(scheduler_traces_path).parent.parent
        if Path(scheduler_traces_path).name == "traces.jsonl":
            traces, w = straces.load_traces(parent)
        else:
            traces, w = _load_jsonl(scheduler_traces_path)
        warnings.extend(w)

    payload = build_closeout(
        generated_at_utc=generated_at_utc,
        live_evidence=ev_recs,
        lane_configs=configs,
        continuity_events=events,
        scheduler_traces=traces,
    )
    payload["inputs"]["live_evidence_sha256"] = _sha256_file(live_evidence_path)
    if continuity_events_path is not None:
        payload["inputs"]["continuity_events_sha256"] = \
            _sha256_file(continuity_events_path)
    if scheduler_traces_path is not None:
        payload["inputs"]["scheduler_traces_sha256"] = \
            _sha256_file(scheduler_traces_path)
    return payload, warnings


def cont_validate(rec: dict[str, Any]) -> bool:
    """True when invalid (kept local to avoid circular import weight)."""
    from .continuity import validate_event
    return bool(validate_event(rec))
