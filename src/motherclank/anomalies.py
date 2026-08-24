"""Motherclank M2 — deterministic anomaly detection (ADR-0002).

Pure functions over Motherclank's own append-only history. No clocks, no
network, no notifications: every timestamp comes from recorded snapshots.

Anomaly record lifecycle: NEW -> ONGOING -> RECOVERED (terminal, retained).
UNKNOWN evidence never proves failure; transitions are only judged between
two KNOWN observations. Adapter failures stay isolated per Clank.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import SNAPSHOT_SCHEMA_VERSION

DETECTION_RULES_VERSION = "m2-r1"
STREAK_THRESHOLD = 3          # consecutive non-ok appearances to raise streak
STALE_RULE_MARKER = "R3"      # M1 recency rule
DRIFT_RELATIONSHIP_BAD = "DIVERGED"

_SEV_ORDER = {"INFO": 0, "MEDIUM": 1, "HIGH": 2}
_NON_OK = {"degraded", "failed", "blocked_zero", "zero_items"}
_KNOWN = _NON_OK | {"ok"}


def _aid(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _status_of(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("status", "")).split(".")[-1].lower()
    return str(getattr(entry, "status", "")).split(".")[-1].lower()


def _sid(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("source_id", ""))
    return str(getattr(entry, "source_id", ""))


def _known(status: str) -> bool:
    return status in _KNOWN


def _mk(anomaly_type: str, severity: str, clank_id: str, subject: str,
        observed_at: str, snapshot_hash: str, detail: str,
        *, first_seen: str | None = None) -> dict[str, Any]:
    key = _aid(anomaly_type, clank_id, subject)
    return {
        "anomaly_id": key,
        "type": anomaly_type,
        "severity": severity,
        "clank_id": clank_id,
        "subject": subject,
        "first_seen": first_seen or observed_at,
        "last_seen": observed_at,
        "lifecycle": "NEW" if not first_seen else "ONGOING",
        "evidence": [{"observed_at": observed_at, "detail": detail}],
        "provenance": {
            "derived_by": f"motherclank-m2/{DETECTION_RULES_VERSION}",
            "snapshot_hash": snapshot_hash,
            "deterministic": True,
        },
    }


def detect(snapshots: list[dict[str, Any]],
           continuity_events: list[dict[str, Any]] | None = None,
           liveness_expectations: list[dict[str, Any]] | None = None,
           scheduler_traces: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Derive the full anomaly ledger from ordered M0 snapshots.

    Deterministic replay: same input list => same output list. The wall clock
    is never consulted; all timestamps come from snapshot harvested_at fields.

    F6: when a continuity registry is supplied, known destructive events are
    (a) emitted as explicit CONTINUITY_EVENT records instead of being read as
    organic source transitions, and (b) used to QUALIFY anomalies derived
    from observations inside a discontinuity window.

    F6b: when an execution-expectations registry is supplied, expected-but-
    unmaterialized runs raise MATERIALIZATION_GAP (an execution-plane fact),
    while intentionally dormant lanes raise nothing.

    P-4: scheduler-fire traces upgrade MATERIALIZATION_GAP derivation from
    run-absence inference to positive pre-exec evidence where traces exist.
    """
    if liveness_expectations:
        from . import liveness as live  # local import: optional dependency path
    else:
        live = None
    # stable ordering by harvest time, then content hash for ties
    snaps = sorted(snapshots,
                   key=lambda s: (s.get("harvested_at_utc", ""),
                                  s.get("content_hash", "")))
    ledger: dict[str, dict[str, Any]] = {}

    def upsert(new: dict[str, Any]) -> None:
        existing = ledger.get(new["anomaly_id"])
        if existing is None:
            if new["last_seen"] != new["first_seen"]:
                new["lifecycle"] = "ONGOING"
            ledger[new["anomaly_id"]] = new
        else:
            if existing["lifecycle"] == "RECOVERED":
                # re-opened after recovery: fresh episode, keep original id
                new["first_seen"] = new["last_seen"]
                new["lifecycle"] = "REOPENED" if False else "NEW"
            else:
                new["first_seen"] = existing["first_seen"]
                new["lifecycle"] = "ONGOING"
            new["evidence"] = (existing["evidence"] + new["evidence"])[-10:]
            ledger[new["anomaly_id"]] = new

    prev_sources: dict[str, dict[str, str]] = {}
    streaks: dict[str, int] = {}
    prev_fleet_state: str | None = None

    for snap in snaps:
        at = snap.get("harvested_at_utc", "")
        shash = snap.get("content_hash", "")

        # --- source-level transitions and streaks -------------------------
        for cid, block in snap.get("clanks", {}).items():
            health = block.get("health") or {}
            if isinstance(health, dict) and health.get("observation") == "FAILED_ADAPTER":
                continue  # partial adapter failure cannot contaminate siblings
            current: dict[str, str] = {}
            for entry in (health.get("sources") or []) if isinstance(health, dict) else []:
                sid = _sid(entry)
                st = _status_of(entry)
                if not sid:
                    continue
                current[sid] = st
                if not _known(st):
                    streaks[(cid, sid)] = 0
                    continue
                streak_key = (cid, sid)
                streaks[streak_key] = streaks.get(streak_key, 0) + 1 if st in _NON_OK else 0

                prev = prev_sources.get(cid, {}).get(sid)
                if prev == "ok" and st in _NON_OK:
                    sev = "HIGH" if st in ("failed", "blocked_zero") else "MEDIUM"
                    upsert(_mk("SOURCE_HEALTH_TRANSITION", sev, cid, sid, at, shash,
                               f"{prev} -> {st}"))
                elif st in _NON_OK:
                    # first KNOWN observation already bad: record as ONGOING-class
                    upsert(_mk("SOURCE_DEGRADED_AT_FIRST_OBSERVATION",
                               "MEDIUM" if st in ("degraded", "zero_items") else "HIGH",
                               cid, sid, at, shash, f"observed {st} with no prior ok"))
                if streaks[streak_key] >= STREAK_THRESHOLD:
                    upsert(_mk("PERSISTENT_BLOCKED_STREAK", "HIGH", cid, sid, at,
                               shash, f"{st} for {streaks[streak_key]} consecutive "
                                      f"observations"))
            prev_sources[cid] = current

            # --- stale-run transition (M1 R3 evidence) --------------------
            synthesis_rules = block.get("_synthesis_rules") or []
            expectation = None
            dormant = False
            if live is not None:
                expectation = live.expectation_for(liveness_expectations, cid, at)
                dormant = bool(expectation) and \
                    expectation.get("policy") in live.DORMANT_POLICIES
            if STALE_RULE_MARKER in synthesis_rules and not dormant:
                upsert(_mk("STALE_RUN", "HIGH", cid, "*", at, shash,
                           "recency rule fired: last successful work older than "
                           "stale window"))
            # --- materialization gap (F6b; pre-exec failure class) --------
            if live is not None and expectation is not None and not dormant:
                trace = None
                if scheduler_traces:
                    from . import scheduler_traces as straces
                    cadence = expectation.get("cadence_seconds")
                    grace = (expectation.get("grace_multiplier")
                             or live.DEFAULT_GRACE_MULTIPLIER)
                    # window=None for multi-cadence lanes: positive traces
                    # still correlate; only the cadence-bounded escalation in
                    # derive_liveness depends on a declared window.
                    window = float(cadence) * float(grace) if cadence else None
                    trace = straces.latest_trace_for(
                        scheduler_traces, cid, before=at, window_seconds=window)
                lv = live.derive_liveness(block, expectation, observed_at=at,
                                          trace=trace)
                if lv["liveness_state"] == "MATERIALIZATION_GAP":
                    ev = lv.get("evidence", {})
                    upsert(_mk("MATERIALIZATION_GAP", "HIGH", cid,
                               "execution", at, shash,
                               f"expected execution did not materialize a run: "
                               f"scheduler evidence age="
                               f"{ev.get('last_invocation_age_seconds', 'UNKNOWN')}s, "
                               f"last run age="
                               f"{ev.get('last_run_age_seconds', 'UNKNOWN')}s; "
                               f"cause NOT attributed (pre-exec failure, timer "
                               f"silence, or observer blindness all possible)"))

        # --- scheduler invocation vs successful work (optional evidence) --
        for cid, block in snap.get("clanks", {}).items():
            pair = block.get("scheduler_pair") or {}
            inv = pair.get("last_scheduler_invocation")
            commit = pair.get("last_successful_job_commit")
            if inv and commit and str(inv) > str(commit):
                upsert(_mk("SCHEDULER_INVOCATION_WITHOUT_WORK", "HIGH", cid,
                           "operational-scheduler", at, shash,
                           f"invocation {inv} newer than successful commit {commit}"))

        # --- deployment/revision drift -----------------------------------
        for row in snap.get("law9_drift") or []:
            if row.get("relationship") == DRIFT_RELATIONSHIP_BAD:
                upsert(_mk("REVISION_DRIFT", "MEDIUM", row["clank"],
                           row.get("checkout_path", "*"), at, shash,
                           f"checkout {row.get('checkout_head', 'UNKNOWN')[:12]} "
                           f"!= ledger {str(row.get('ledger_sha'))[:12]}"))

        # --- fleet-health degradation -------------------------------------
        synth_state = snap.get("_fleet_state")
        if synth_state:
            if prev_fleet_state in ("HEALTHY",) and synth_state in ("DEGRADED", "FAILED"):
                upsert(_mk("FLEET_HEALTH_DEGRADATION",
                           "HIGH" if synth_state == "FAILED" else "MEDIUM",
                           "fleet", "*", at, shash,
                           f"{prev_fleet_state} -> {synth_state}"))
            prev_fleet_state = synth_state

    # --- recovery pass ------------------------------------------------------
    latest_bad_keys = set()
    for a in ledger.values():
        pass
    # an anomaly is RECOVERED when its subject's most recent observation is ok
    # (or, for state classes, when the triggering condition is absent in the
    # final snapshot). Rebuild from final snapshot for precision:
    final = snaps[-1] if snaps else None
    if final:
        for a in ledger.values():
            cid, subject = a["clank_id"], a["subject"]
            block = final.get("clanks", {}).get(cid)
            recovered = False
            if block is None:
                recovered = True
            elif a["type"] in ("SOURCE_HEALTH_TRANSITION",
                               "SOURCE_DEGRADED_AT_FIRST_OBSERVATION",
                               "PERSISTENT_BLOCKED_STREAK"):
                health = block.get("health") or {}
                entries = (health.get("sources") or []) if isinstance(health, dict) else []
                match = [e for e in entries if _sid(e) == subject]
                if not match:
                    recovered = True
                else:
                    recovered = _status_of(match[0]) == "ok"
            elif a["type"] == "STALE_RUN":
                rules = block.get("_synthesis_rules") or []
                recovered = STALE_RULE_MARKER not in rules
            elif a["type"] == "MATERIALIZATION_GAP" and live is not None:
                expectation = live.expectation_for(liveness_expectations, cid,
                                                   final.get("harvested_at_utc", ""))
                lv = (live.derive_liveness(block, expectation,
                                           observed_at=final.get("harvested_at_utc", ""))
                      if expectation else {"liveness_state": "UNKNOWN"})
                recovered = lv["liveness_state"] != "MATERIALIZATION_GAP"
            elif a["type"] == "REVISION_DRIFT":
                rows = [r for r in (final.get("law9_drift") or [])
                        if r.get("clank") == cid]
                recovered = (not rows) or rows[0].get("relationship") != DRIFT_RELATIONSHIP_BAD
            elif a["type"] == "FLEET_HEALTH_DEGRADATION":
                recovered = final.get("_fleet_state") == "HEALTHY"
            if recovered and a["lifecycle"] != "RECOVERED":
                a["lifecycle"] = "RECOVERED"
                a["recovered_at"] = final.get("harvested_at_utc", "")
                a["recovery_snapshot_hash"] = final.get("content_hash")

    out = sorted(ledger.values(), key=lambda a: (a["type"], a["clank_id"], a["subject"]))
    for a in out:
        a["chain_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(a, sort_keys=True, default=str).encode()).hexdigest()
    if continuity_events:
        from . import continuity as cont
        incidents = cont.incident_records(continuity_events)
        qualified = cont.qualify_anomalies(out, snaps, continuity_events)
        # Incidents sort alongside; qualification is additive evidence only.
        combined: dict[str, dict[str, Any]] = {a["chain_hash"]: a for a in qualified}
        for inc in incidents:
            existing = combined.get(inc["chain_hash"])
            if existing is not None:
                continue
            combined[inc["chain_hash"]] = inc
        return sorted(combined.values(),
                      key=lambda a: (a["type"], a["clank_id"], a["subject"]))
    return out


def load_history(var_dir: Path) -> list[dict[str, Any]]:
    """Load ordered snapshots annotated with their matching synthesis data:
    per-clank rules (_synthesis_rules) and fleet state (_fleet_state)."""
    snap_dir = var_dir / "snapshots"
    synth_dir = var_dir / "syntheses"
    snaps = []
    if snap_dir.exists():
        for file in sorted(snap_dir.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                if line.strip():
                    try:
                        snaps.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    synths = {}
    if synth_dir.exists():
        for file in sorted(synth_dir.glob("*.jsonl")):
            for line in file.read_text().splitlines():
                if line.strip():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = rec.get("snapshot_hash")
                    if key:
                        synths[key] = rec
    for s in snaps:
        match = synths.get(s.get("content_hash"))
        rules_map = {}
        fleet_state = None
        if match:
            fleet_state = match.get("fleet_state")
            for cid, claim in (match.get("clanks") or {}).items():
                rules_map[cid] = claim.get("rules_applied") or []
        for cid, block in (s.get("clanks") or {}).items():
            block["_synthesis_rules"] = rules_map.get(cid, [])
        s["_fleet_state"] = fleet_state
    return snaps


def chain_hash(records: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()


def previous_chain_hash(out_dir: Path) -> str | None:
    d = out_dir / "anomalies"
    if not d.exists():
        return None
    latest, latest_key = None, ""
    for file in sorted(d.glob("*.jsonl")):
        for line in file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(rec.get("batch_generated_from", ""))
            if key >= latest_key and rec.get("batch_hash"):
                latest_key, latest = key, rec["batch_hash"]
    return latest


def append_batch(out_dir: Path, batch: dict[str, Any]) -> Path:
    day = str(batch["batch_generated_from"])[:10]
    d = out_dir / "anomalies"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(batch, sort_keys=True, default=str) + "\n")
    return d / f"{day}.jsonl"


def build_batch(out_dir: Path, snapshots: list[dict[str, Any]],
                anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    generated_from = max((s.get("harvested_at_utc", "") for s in snapshots),
                         default="")
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": "DERIVED anomaly ledger — Motherclank M2; deterministic rules only",
        "batch_generated_from": generated_from,
        "detection_rules_version": DETECTION_RULES_VERSION,
        "previous_batch_hash": previous_chain_hash(out_dir),
        "active_count": sum(1 for a in anomalies if a["lifecycle"] in ("NEW", "ONGOING")),
        "recovered_count": sum(1 for a in anomalies if a["lifecycle"] == "RECOVERED"),
        "anomalies": anomalies,
    }
    payload["batch_hash"] = chain_hash(anomalies)
    return payload
