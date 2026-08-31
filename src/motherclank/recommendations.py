"""Motherclank M3 — deterministic operator recommendations (ADR-0002).

Text-only, advisory, operator-owned. Derived purely from the M2 anomaly
ledger: one rule table maps anomaly classes to a fixed recommendation
template. No LLM reasoning, no delivery, no execution, no authority.

Lifecycle/dedup contract:
- recommendation_id is a stable hash of (rule_key, clank_id, subject_group);
  repeated anomalies UPDATE the existing recommendation's citations instead
  of spawning duplicates;
- when EVERY cited anomaly is RECOVERED the recommendation becomes CLOSED
  (retained, never deleted); mixed sets stay ACTIVE with recovered citations
  moved to resolved_citations;
- UNKNOWN-only inputs yield nothing; no template may propose destructive or
  mutating action (enforced by tests).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import SNAPSHOT_SCHEMA_VERSION

RULES_VERSION = "m3-r1"

CATEGORIES = ("INVESTIGATION", "UPSTREAM_CLANK_REMEDIATION",
              "DEPLOYMENT_SCHEDULER_INSPECTION", "NO_ACTION_WATCH")
STATUSES = ("ACTIVE", "CLOSED")

# Rule table: anomaly_type -> (rule_key, category, priority, title/action template)
_RULES: dict[str, dict[str, str]] = {
    "PERSISTENT_BLOCKED_STREAK": {
        "category": "UPSTREAM_CLANK_REMEDIATION",
        "priority": "P1",
        "title": "Persistent blocked/degraded streak on {subject} ({clank})",
        "action": ("Inspect the upstream source availability for '{subject}' in "
                   "{clank}; review the Clank's own health notes and backoff state, "
                   "then decide promotion/rollback of that source inside the Clank."),
    },
    "SOURCE_HEALTH_TRANSITION": {
        "category": "INVESTIGATION",
        "priority": "P2",
        "title": "Source transition observed on {subject} ({clank})",
        "action": ("Investigate the recorded transition for '{subject}' using the "
                   "cited evidence; confirm whether the Clank's source-health "
                   "interpretation matches reality."),
    },
    "SOURCE_DEGRADED_AT_FIRST_OBSERVATION": {
        "category": "NO_ACTION_WATCH",
        "priority": "P3",
        "title": "{subject} ({clank}) observed non-OK with no prior ok history",
        "action": ("Watch '{subject}'. First-known observation was already non-OK; "
                   "no earlier healthy baseline exists to compare against. Escalate "
                   "only if a streak rule fires."),
    },
    "STALE_RUN": {
        "category": "DEPLOYMENT_SCHEDULER_INSPECTION",
        "priority": "P1",
        "title": "Stale run detected on {clank}",
        "action": ("Inspect scheduler state and last successful work for {clank}: "
                   "compare timer/cron identity, invocation records and the Clank's "
                   "own run history before touching anything."),
    },
    # F6b: expected execution produced no run. This is an execution-plane
    # fact; pre-exec shell/redirect failure means the collector never ran,
    # so collector-regression diagnosis is forbidden.
    "MATERIALIZATION_GAP": {
        "category": "DEPLOYMENT_SCHEDULER_INSPECTION",
        "priority": "P1",
        "title": "Expected execution did not materialize a run on {clank}",
        "action": ("Inspect the execution path for {clank} in order: scheduler "
                   "fired? process started? shell/pre-exec steps (redirects, "
                   "permissions, working directory) succeeded? Only after a "
                   "materialized run is confirmed may application-level health "
                   "be interpreted. Do NOT diagnose collector regression from "
                   "this record alone."),
    },
    "SCHEDULER_INVOCATION_WITHOUT_WORK": {
        "category": "DEPLOYMENT_SCHEDULER_INSPECTION",
        "priority": "P1",
        "title": "Scheduler invocations without successful work on {clank}",
        "action": ("Inspect {clank}'s scheduler evidence: invocation timestamps are "
                   "advancing while successful job commits are not. Check task "
                   "identity, working directory and application logs."),
    },
    "REVISION_DRIFT": {
        "category": "DEPLOYMENT_SCHEDULER_INSPECTION",
        "priority": "P2",
        "title": "Deployment revision drift on {clank}",
        "action": ("Compare the checkout HEAD with the fleet-ledger SHA for {clank} "
                   "(Law 9). Converge via the normal reviewed deploy process when "
                   "confirmed."),
    },
    "FLEET_HEALTH_DEGRADATION": {
        "category": "INVESTIGATION",
        "priority": "P1",
        "title": "Fleet health degraded ({detail_short})",
        "action": ("Review the cited per-source anomalies below; they fully explain "
                   "this fleet-level signal."),
    },
    # F6: known continuity incidents surface as watch-only records citing the
    # incident — never as upstream-collector repair instructions.
    "CONTINUITY_EVENT": {
        "category": "NO_ACTION_WATCH",
        "priority": "P3",
        "title": "Known observational discontinuity on {clank} ({subject})",
        "action": ("A registered continuity event explains observations for "
                   "{clank} during this window. Do NOT interpret pre/post-incident "
                   "differences as organic source behaviour; see the cited "
                   "continuity evidence."),
    },
}

_RECOVERED_TYPES_AUTONOMOUS = {"STALE_RUN"}  # closed episodes become watch items


def _rid(rule_key: str, clank_id: str, subject_group: str) -> str:
    raw = "|".join((rule_key, clank_id, subject_group))
    return "rec-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cite(a: dict[str, Any]) -> dict[str, Any]:
    latest = (a.get("evidence") or [{}])[-1]
    citation = {
        "anomaly_id": a["anomaly_id"],
        "type": a["type"],
        "severity": a["severity"],
        "lifecycle": a["lifecycle"],
        "first_seen": a["first_seen"],
        "last_seen": a["last_seen"],
        "latest_evidence": latest.get("detail", ""),
    }
    # F6: carry continuity qualification into citations so recommendations
    # name the explaining incident instead of implying organic behaviour.
    if a.get("continuity_qualified"):
        citation["continuity_qualified"] = True
        citation["continuity_state"] = a.get("continuity_state", "")
        citation["continuity_event_ids"] = a.get("continuity_event_ids", [])
    return citation


def derive_recommendations(anomaly_batch: dict[str, Any]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    generated_from = anomaly_batch.get("batch_generated_from", "")
    input_hash = anomaly_batch.get("batch_hash", "")

    for anomaly in anomaly_batch.get("anomalies", []):
        atype = anomaly.get("type", "")
        lifecycle = anomaly.get("lifecycle", "")

        # Recovered episodes: self-limiting classes become NO_ACTION_WATCH;
        # other classes resolve through their own rule but land CLOSED when
        # every citation they contributed is recovered.
        if lifecycle == "RECOVERED" and atype in _RECOVERED_TYPES_AUTONOMOUS:
            rule_key = f"watch:{atype}"
            category, priority = "NO_ACTION_WATCH", "P3"
        else:
            rule = _RULES.get(atype)
            if rule is None:
                continue  # unmapped anomaly types never invent advice
            rule_key = atype
            category, priority = rule["category"], rule["priority"]

        clank_id = anomaly.get("clank_id", "")
        subject = anomaly.get("subject", "*")
        # subject-grouping keeps recurring subjects under one recommendation
        subject_group = "*" if subject == "*" else subject

        rid = _rid(rule_key, clank_id, subject_group)
        rec = out.get(rid)
        citation = _cite(anomaly)

        if rec is None:
            if lifecycle == "RECOVERED":
                status = "CLOSED"
            elif category == "NO_ACTION_WATCH":
                status = "ACTIVE"
            else:
                status = "ACTIVE"
            template = (_RULES.get(atype) or {
                "title": "Recovered: {subject} ({clank})",
                "action": ("Watch only: the triggering episode has recovered. "
                           "Re-open if it recurs."),
            })
            rec = {
                "recommendation_id": rid,
                "status": status,
                "category": category,
                "priority": priority,
                "rule_key": rule_key,
                "clank_id": clank_id,
                "subject": subject,
                "title": template["title"].format(
                    subject=subject, clank=clank_id,
                    detail_short=str(anomaly.get("evidence", [{}])[-1].get("detail", ""))[:40]),
                "recommended_action": template["action"].format(
                    subject=subject, clank=clank_id),
                "cited_anomalies": [],
                "resolved_citations": [],
                "first_seen": anomaly.get("first_seen"),
                "provenance": {
                    "derived_by": f"motherclank-m3/{RULES_VERSION}",
                    "deterministic": True,
                    "anomaly_batch_hash": input_hash,
                    "advisory_only": True,
                },
                "generated_from": generated_from,
            }
        else:
            rec["generated_from"] = generated_from
            rec["provenance"]["anomaly_batch_hash"] = input_hash

        if lifecycle == "RECOVERED":
            targets = rec["resolved_citations"]
        else:
            targets = rec["cited_anomalies"]
            rec["first_seen"] = min(filter(None, (rec["first_seen"],
                                                  anomaly.get("first_seen"))))
        if not any(c["anomaly_id"] == citation["anomaly_id"] for c in targets):
            targets.append(citation)

        # F6: continuity-incident recommendations track their own OPEN/CLOSED
        # state and are exempt from organic active/recovered re-evaluation.
        if atype == "CONTINUITY_EVENT":
            rec["status"] = "ACTIVE" if lifecycle == "OPEN" else "CLOSED"
            out[rid] = rec
            continue

        # lifecycle re-evaluation: closed only when every citation is recovered
        active_siblings = [c for c in rec["cited_anomalies"]]
        if rec["cited_anomalies"] == [] and rec["resolved_citations"]:
            rec["status"] = "CLOSED"
        elif any(c["lifecycle"] != "RECOVERED" for c in active_siblings):
            rec["status"] = "ACTIVE"
        elif active_siblings:
            rec["status"] = "ACTIVE"
        out[rid] = rec

    result = sorted(out.values(),
                    key=lambda r: (r["status"], r["priority"], r["clank_id"]))
    for rec in result:
        rec["chain_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
    return result


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def read_latest_anomaly_batch(var_dir: Path) -> dict[str, Any] | None:
    d = var_dir / "anomalies"
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
            if key >= latest_key:
                latest_key, latest = key, rec
    return latest


def previous_batch_hash(out_dir: Path) -> str | None:
    d = out_dir / "recommendations"
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
            key = str(rec.get("generated_from", ""))
            if key >= latest_key and rec.get("batch_hash"):
                latest_key, latest = key, rec["batch_hash"]
    return latest


def append_batch(out_dir: Path, batch: dict[str, Any]) -> Path:
    day = str(batch["generated_from"])[:10]
    d = out_dir / "recommendations"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(batch, sort_keys=True, default=str) + "\n")
    return d / f"{day}.jsonl"


def build_batch(out_dir: Path, anomaly_batch: dict[str, Any],
                recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": ("DERIVED operator recommendations — Motherclank M3; "
                          "advisory only, operator-owned"),
        "generated_from": anomaly_batch.get("batch_generated_from", ""),
        "rules_version": RULES_VERSION,
        "previous_batch_hash": previous_batch_hash(out_dir),
        "anomaly_batch_hash": anomaly_batch.get("batch_hash"),
        "active_count": sum(1 for r in recommendations if r["status"] == "ACTIVE"),
        "closed_count": sum(1 for r in recommendations if r["status"] == "CLOSED"),
        "recommendations": recommendations,
    }
    payload["batch_hash"] = content_hash(payload)
    return payload
