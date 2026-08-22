"""ADR-0003 §2 — bridge Motherclank M3 recommendations into Diagnostic Clank's
Agent Inbox through its public contract, unchanged.

Per reviewed contract:
- output_type   = OutputType.RECOMMENDATION
- agent_family  = AgentFamily.MISC            (no MOTHERCLANK enum value)
- misc_source   = "motherclank-m3/<RULES_VERSION>"
- external_ref  = recommendation_id           (stable logical identity)
- primary_clank_id = affected clank; "fleet-wide" only for genuinely fleet-wide rows

Deterministic rendered text is the raw_text payload; identical emissions dedup
(content hash), changed evidence under one recommendation_id forms an immutable
version series. This module performs no writes other than via the Inbox API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters import ensure_adapter_plane

MISC_SOURCE_PREFIX = "motherclank-m3/"


def render_recommendation_text(rec: dict[str, Any], batch: dict[str, Any]) -> str:
    """Deterministic raw_text rendering of one recommendation.

    Pure function of (rec, batch) — same inputs always yield byte-identical
    text so Inbox content-hash dedup behaves predictably."""
    cites = "; ".join(
        f"{c['anomaly_id']}[{c['lifecycle']}] {c['latest_evidence']}"
        for c in rec.get("cited_anomalies", [])
    )
    resolved = "; ".join(
        f"{c['anomaly_id']}[RECOVERED] {c['latest_evidence']}"
        for c in rec.get("resolved_citations", [])
    )
    prov = rec.get("provenance", {})
    lines = [
        f"RECOMMENDATION {rec['recommendation_id']}",
        f"title: {rec['title']}",
        f"clank: {rec['clank_id']}",
        f"subject: {rec.get('subject', '*')}",
        f"status: {rec['status']} priority: {rec['priority']} category: {rec['category']}",
        f"first_seen: {rec.get('first_seen')}",
        f"action: {rec['recommended_action']}",
        f"cited_anomalies: {cites or 'none'}",
        f"resolved_citations: {resolved or 'none'}",
        f"rules: {prov.get('derived_by', MISC_SOURCE_PREFIX + 'unknown')}",
        f"deterministic: {prov.get('deterministic')} advisory_only: {prov.get('advisory_only')}",
        f"chain_hash: {rec.get('chain_hash')}",
        f"generated_from: {rec.get('generated_from')} batch_hash: {batch.get('batch_hash')}",
        "",
        "ADVISORY ONLY — operator owns every decision; Motherclank executes nothing.",
    ]
    return "\n".join(lines)


def bridge_recommendations(batch: dict[str, Any], *, inbox_db_path: Path,
                           registry, diagnostic_clank_src: Path | None = None,
                           rules_version: str = "") -> dict[str, Any]:
    """Save every recommendation in an M3 batch to the Agent Inbox.

    Returns a summary dict; raises nothing on individual record failure is NOT
    attempted — a bridge failure aborts loudly before any commit of partial
    state beyond what the Inbox itself persisted (each save commits its own row,
    matching Inbox save semantics).
    """
    if diagnostic_clank_src is not None:
        ensure_adapter_plane(diagnostic_clank_src)
    else:
        ensure_adapter_plane()

    from clank_runtime.knowledge.inbox import AgentFamily, AgentOutputInbox, OutputType

    inbox = AgentOutputInbox(inbox_db_path, registry)
    try:
        misc_source = MISC_SOURCE_PREFIX + (rules_version or str(
            batch.get("rules_version") or "unknown"))
        saved, deduped = [], []
        for rec in batch.get("recommendations", []):
            text = render_recommendation_text(rec, batch)
            clank_id = rec.get("clank_id")
            # ADR-0003 fail-closed identity: every M3 recommendation names its
            # affected Clank. Missing/empty/blank identity is malformed input
            # and must abort the bridge — it is never rewritten to
            # 'fleet-wide', which would misattribute a broken record.
            if not isinstance(clank_id, str) or not clank_id.strip():
                raise ValueError(
                    f"malformed recommendation identity: {rec.get('recommendation_id')!r} "
                    f"has invalid clank_id {clank_id!r}")
            rec_out = inbox.save(
                agent_family=AgentFamily.MISC,
                primary_clank_id=clank_id,
                raw_text=text,
                output_type=OutputType.RECOMMENDATION,
                related_clank_ids=[clank_id],
                misc_source=misc_source,
                session_label=batch.get("batch_hash"),
                external_ref=rec["recommendation_id"],
                _duplicate_of=deduped,
            )
            saved.append({"recommendation_id": rec["recommendation_id"],
                          "output_id": rec_out.output_id})
        return {"saved": saved, "deduplicated": len(deduped),
                "misc_source": misc_source}
    finally:
        inbox.close()
