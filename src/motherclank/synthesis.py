"""Motherclank M1 — deterministic fleet-health synthesis (ADR-0002).

Consumes an M0 snapshot payload; derives per-Clank states and a fleet rollup.
Binding rules (ordered, first match wins):

  R0  any adapter-failure block in the Clank's observation      -> UNKNOWN
      (adapter plane could not evidence the system; never guessed around)
  R1  status.operational_state == failed                        -> FAILED
  R2  source rollup: every recorded source failed/blocked_zero -> FAILED
      some failed/blocked_zero                                  -> DEGRADED
  R3  last successful/attempted run older than stale_hours      -> UNKNOWN (stale)
      (recency can only downgrade)
  R4  status.operational_state == degraded                      -> DEGRADED
  R5  status.operational_state == healthy                       -> HEALTHY
  R6  otherwise                                                 -> UNKNOWN

Downgrade-only property: no rule may raise a state above what mandatory
evidence supports; UNKNOWN inputs force UNKNOWN or worse, never HEALTHY.

Every derived claim carries provenance: clank_id, snapshot observed_at,
and the exact evidence fields consulted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import SNAPSHOT_SCHEMA_VERSION

STATES = ("HEALTHY", "DEGRADED", "FAILED", "UNKNOWN")
_SEVERITY = {"HEALTHY": 0, "DEGRADED": 1, "FAILED": 2, "UNKNOWN": 3}


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _worst(a: str, b: str) -> str:
    """Worse of two states. UNKNOWN is the most severe *uncertainty* outcome:
    it must never be upgraded away by healthy-looking evidence elsewhere."""
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _source_rollup(health: dict[str, Any]) -> dict[str, int] | None:
    sources = health.get("sources") if isinstance(health, dict) else None
    if not sources:
        return None
    counts = {"ok": 0, "degraded": 0, "failed": 0, "blocked_zero": 0, "unknown": 0}
    for s in sources:
        raw = s.get("status", "") if isinstance(s, dict) else getattr(s, "status", "")
        key = str(raw).split(".")[-1].lower()
        counts[key if key in counts else "unknown"] += 1
    return counts


def synthesize_clank(clank_id: str, block: dict[str, Any],
                     *, observed_at: str, stale_hours: float,
                     continuity: dict[str, Any] | None = None,
                     liveness: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence: list[str] = []
    state = "UNKNOWN"

    def claim(final: str, rules: list[str]) -> dict[str, Any]:
        return {
            "clank_id": clank_id,
            "state": final,
            "rules_applied": rules,
            "evidence_fields": evidence,
            "observed_at": observed_at,
            "provenance": {
                "derived_by": "motherclank-m1",
                "source_clank": clank_id,
                "snapshot_observed_at": observed_at,
            },
        }

    def note(path: str) -> None:
        evidence.append(path)

    # R0 — adapter failures poison everything we could claim
    for key, val in block.items():
        if isinstance(val, dict) and val.get("observation") == "FAILED_ADAPTER":
            note(f"clanks.{clank_id}.{key}.observation=FAILED_ADAPTER")
            return claim("UNKNOWN", ["R0"])

    rules: list[str] = []
    status = block.get("status") or {}
    op_state = str(status.get("operational_state", "")).split(".")[-1].lower()
    note(f"clanks.{clank_id}.status.operational_state={op_state or 'MISSING'}")

    health = block.get("health") or {}
    rollup = _source_rollup(health)
    if rollup is None:
        note(f"clanks.{clank_id}.health.sources=EMPTY_OR_ABSENT")
    else:
        note(f"clanks.{clank_id}.health.sources={json.dumps(rollup, sort_keys=True)}")

    last_run = block.get("last_run") or {}
    finished = last_run.get("finished_at") or last_run.get("completed_at") \
        or last_run.get("started_at")
    last_dt = _parse(finished)
    if last_dt:
        note(f"clanks.{clank_id}.last_run.finished_at={finished}")

    # R1/R4/R5 from declared operational state
    declared = {"failed": "FAILED", "degraded": "DEGRADED", "healthy": "HEALTHY"}.get(op_state)

    # R2 from source rollup
    src_state = None
    if rollup is not None:
        total = sum(rollup.values())
        bad = rollup["failed"] + rollup["blocked_zero"]
        if total and bad == total:
            src_state = "FAILED"
        elif bad:
            src_state = "DEGRADED"

    # R3 recency downgrade (only ever downgrades).
    # Reference time is the SNAPSHOT's own harvest instant, never wall
    # clock: derived claims must be a deterministic function of the
    # snapshot they claim about, and replays of historical snapshots must
    # not silently become stale.
    ref_dt = _parse(observed_at) if observed_at else None
    if ref_dt is None:
        ref_dt = datetime.now(timezone.utc)
    recency_state = None
    if last_dt is not None:
        age_h = (ref_dt - last_dt).total_seconds() / 3600.0
        if age_h > stale_hours:
            recency_state = "UNKNOWN"
            note(f"recency.age_hours>{stale_hours} ({age_h:.1f}h)")
    elif op_state in ("healthy", "degraded") and str(
            (status.get("extensions") or {}).get("recency_policy", "")
    ).upper() != "NONE":
        recency_state = "UNKNOWN"
        note("recency.no_run_timestamp_available")
    elif op_state in ("healthy", "degraded"):
        note("recency.policy=NONE")

    # Combine: worst of (declared, source-evidence); UNKNOWN when neither
    # evidences anything; recency then may ONLY downgrade to UNKNOWN.
    candidates = [c for c in (declared, src_state) if c is not None]
    if declared is not None:
        rules.append({"FAILED": "R1", "DEGRADED": "R4", "HEALTHY": "R5"}[declared])
    if src_state is not None:
        rules.append("R2")
    if candidates:
        state = max(candidates, key=lambda st: _SEVERITY[st])
    else:
        state = "UNKNOWN"
        rules.append("R6")
    if recency_state == "UNKNOWN":
        state = "UNKNOWN"
        rules.append("R3")

    claim_obj = claim(state, sorted(set(rules)) or ["R6"])
    # F6/F6b: continuity AND execution-liveness are ORTHOGONAL dimensions.
    # They never upgrade or downgrade the operational state machine; they
    # qualify what an operational state may be assumed to mean. Sources, in
    # order: explicit context argument (registry-derived), else harvest-time
    # annotations already carried on the snapshot block.
    if continuity is None and isinstance(block.get("continuity"), dict):
        continuity = block["continuity"]
    if liveness is None and isinstance(block.get("liveness"), dict):
        liveness = block["liveness"]
    if continuity is not None:
        claim_obj["continuity"] = {
            "continuity_state": continuity.get("continuity_state", "UNKNOWN_CONTINUITY"),
            "epoch_id": continuity.get("epoch_id", "UNKNOWN"),
            "active_event_ids": continuity.get("active_event_ids", []),
            "evidence_refs": continuity.get("evidence_refs", []),
            "orthogonal_to_operational_state": True,
        }
    if liveness is not None:
        claim_obj["liveness"] = {
            "liveness_state": liveness.get("liveness_state", "UNKNOWN"),
            "policy": liveness.get("policy", "UNKNOWN"),
            "stages": liveness.get("stages", {}),
            "evidence": liveness.get("evidence", {}),
            "orthogonal_to_operational_health": True,
        }
    if liveness is not None:
        claim_obj["liveness"] = {
            "liveness_state": liveness.get("liveness_state", "UNKNOWN"),
            "policy": liveness.get("policy", "UNKNOWN"),
            "stages": liveness.get("stages", {}),
            "evidence": liveness.get("evidence", {}),
            "orthogonal_to_operational_health": True,
        }
    # Adapter-declared capability statements ride along verbatim: they are
    # evidence about the lane, not derived claims, so no interpretation here.
    if isinstance(block.get("capability_states"), dict):
        claim_obj["capability_states"] = block["capability_states"]
    return claim_obj


def synthesize_fleet(snapshot_payload: dict[str, Any], *,
                     stale_hours: float = 24.0,
                     continuity_events: list[dict[str, Any]] | None = None,
                     liveness_expectations: list[dict[str, Any]] | None = None,
                     liveness_grace_multiplier: float = 2.0,
                     scheduler_traces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    clanks_out = {}
    counts = {s: 0 for s in STATES}
    contexts: dict[str, dict[str, Any]] = {}
    liveness_ctx: dict[str, dict[str, Any]] = {}
    observed_at = snapshot_payload.get("harvested_at_utc", "")
    if continuity_events:
        from . import continuity as cont
        for cid in snapshot_payload.get("clanks", {}):
            contexts[cid] = cont.continuity_context(continuity_events, cid, observed_at)
    if liveness_expectations:
        from . import liveness as live
        for cid, block in snapshot_payload.get("clanks", {}).items():
            exp = live.expectation_for(liveness_expectations, cid, observed_at)
            trace = None
            if scheduler_traces and exp:
                from . import scheduler_traces as straces
                cadence = exp.get("cadence_seconds")
                grace = exp.get("grace_multiplier") or liveness_grace_multiplier
                # window=None for multi-cadence lanes: traces still correlate
                # (bounded only by observation time), never dropped on the
                # floor just because no single cadence is declared.
                window = float(cadence) * float(grace) if cadence else None
                trace = straces.latest_trace_for(
                    scheduler_traces, cid, before=observed_at,
                    window_seconds=window)
            liveness_ctx[cid] = live.derive_liveness(
                block, exp, observed_at=observed_at,
                grace_multiplier=liveness_grace_multiplier,
                trace=trace)
    for cid in sorted(snapshot_payload.get("clanks", {})):
        result = synthesize_clank(
            cid, snapshot_payload["clanks"][cid],
            observed_at=observed_at,
            stale_hours=stale_hours,
            continuity=contexts.get(cid),
            liveness=liveness_ctx.get(cid),
        )
        clanks_out[cid] = result
        counts[result["state"]] += 1

    # P-4.3: typed evidence envelopes flow through the generic consumer
    # registry. Known types with registered consumers yield derived claims;
    # unknown/malformed/unsupported-major evidence stays visible WITHOUT
    # producing any invented claim.
    envelopes: list[dict[str, Any]] = []
    for block in snapshot_payload.get("clanks", {}).values():
        raw = block.get("evidence_envelopes")
        if isinstance(raw, list):
            envelopes.extend(e for e in raw if isinstance(e, dict))
    evidence_derivation = None
    if envelopes:
        from . import evidence as ev
        derivation = ev.consume_all(envelopes)
        evidence_derivation = derivation
        for claim in derivation.get("derived_claims", []):
            cid = (claim.get("subject") or {}).get("clank_id")
            if cid in clanks_out:
                clanks_out[cid].setdefault("evidence_derived_claims", []) \
                    .append(claim)

    known_states = [c["state"] for c in clanks_out.values() if c["state"] != "UNKNOWN"]
    fleet_state = max(known_states, key=lambda s: _SEVERITY[s]) if known_states else "UNKNOWN"
    confidence = "FULL" if counts["UNKNOWN"] == 0 else "PARTIAL"

    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": "DERIVED fleet synthesis — Motherclank M1; downgrade-only",
        "synthesized_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_hash": snapshot_payload.get("content_hash"),
        "snapshot_harvested_at": snapshot_payload.get("harvested_at_utc"),
        "fleet_state": fleet_state,
        "fleet_confidence": confidence,
        "state_counts": counts,
        "clanks": clanks_out,
        "law9_drift": [],
    }
    if continuity_events:
        from . import continuity as cont
        payload["continuity_registry_hash"] = (
            snapshot_payload.get("continuity_registry_hash")
            or cont.registry_hash(continuity_events))
    if evidence_derivation is not None:
        payload["evidence_derivation"] = {
            "derived_claim_count": len(evidence_derivation.get(
                "derived_claims", [])),
            "unknown_evidence": evidence_derivation.get("unknown_evidence",
                                                        []),
            "observed_envelope_count": evidence_derivation.get("count", 0),
        }
    return payload


def attach_law9_drift(synthesis: dict[str, Any], drift_rows: list[dict[str, Any]]) -> None:
    """Law 9 metric rows: {clank, checkout_head, ledger_sha, relationship}.
    relationship ∈ CONVERGED | CHECKOUT_BEHIND_LEDGER | CHECKOUT_AHEAD_OF_LEDGER | UNKNOWN."""
    synthesis["law9_drift"] = drift_rows


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def previous_synthesis_hash(out_dir: Path) -> str | None:
    """Accepts the M0/M1 output directory (contains syntheses/)."""
    synth_dir = out_dir / "syntheses"
    latest, latest_key = None, ""
    if synth_dir.exists():
        for file in sorted(synth_dir.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(rec.get("synthesized_at_utc", ""))
                if key >= latest_key and rec.get("content_hash"):
                    latest_key, latest = key, rec["content_hash"]
    return latest


def append_synthesis(out_dir: Path, payload: dict[str, Any]) -> Path:
    day = payload["synthesized_at_utc"][:10]
    directory = out_dir / "syntheses"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return directory / f"{day}.jsonl"


def read_latest_snapshot(var_dir: Path) -> dict[str, Any] | None:
    snap_dir = var_dir / "snapshots"
    if not snap_dir.exists():
        return None
    latest, latest_key = None, ""
    for file in sorted(snap_dir.glob("*.jsonl")):
        for line in file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(rec.get("harvested_at_utc", ""))
            if key >= latest_key:
                latest_key, latest = key, rec
    return latest
