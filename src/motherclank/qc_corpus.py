"""Motherclank M4 — QC corpus ingestion (ADR-0002).

Builds an append-only, provenance-rich corpus of HUMAN QC decisions from the
three QC-bearing Clanks, via their read-only adapters, unchanged in spirit:

- raw dispositions preserved verbatim; fleet-normalized disposition added
  ONLY where the mapping is explicit (see FLEET_MAPPING); everything else
  stays UNMAPPED — including missing feedback, which is never reinterpreted
- every record: clank_id, source_table, original_record_id, subject/evidence
  linkage, timestamps, adapter contract version, ingestion snapshot hash,
  content_hash, and lineage (supersedes/superseded_by) for upstream
  corrections — corrections create NEW records; history is never rewritten
- dedupe across ingestion runs by corpus_id+content_hash without deletion

This is a future LEARNING CORPUS, not a learning system: no training, no
collector-behaviour changes, no influence on M1/M2/M3 outputs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import SNAPSHOT_SCHEMA_VERSION

RULES_VERSION = "m4-r1"

FLEET_MAPPING = {
    "useful": "USEFUL",
    "not_useful": "NOT_USEFUL",
    "duplicate": "DUPLICATE",
    "false_positive": "FALSE_POSITIVE",
    # watch-specific availability disposition, defensible as its own value:
    "out_of_stock": "OUT_OF_STOCK",
}
UNMAPPED = "UNMAPPED"


def _content_hash(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _corpus_id(clank_id: str, source_table: str, original_record_id: Any) -> str:
    raw = f"{clank_id}|{source_table}|{original_record_id}"
    return "qc-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_disposition(raw: Any) -> str:
    if raw is None or str(raw).strip() == "":
        return UNMAPPED
    return FLEET_MAPPING.get(str(raw).strip().lower(), UNMAPPED)


def ingest_clank(clank_id: str, adapter: Any, *,
                 ingestion_snapshot_hash: str) -> dict[str, Any]:
    """Collect one Clank's QC rows. Adapter failure stays isolated."""
    block: dict[str, Any] = {"clank_id": clank_id, "records": []}
    try:
        rows = adapter.qc_records()
    except Exception as exc:
        block["observation"] = "FAILED_ADAPTER"
        block["error"] = f"{type(exc).__name__}: {exc}"
        return block
    from clank_runtime.version import ADAPTER_CONTRACT_VERSION  # noqa: PLC0415
    block["adapter_contract_version"] = ADAPTER_CONTRACT_VERSION
    try:
        block["eligible"] = adapter.eligible_count()
    except Exception as exc:
        block["eligible"] = {"eligible_total": None, "error": str(exc)}
    reviewed = len(block["records"])
    block["reviewed_total"] = reviewed
    corrections = sum(1 for r in block["records"]
                      if r.get("is_corrected_upstream") or r.get("supersedes"))
    block["correction_count"] = corrections

    for row in rows:
        raw = row.get("raw_disposition")
        record = {
            "clank_id": clank_id,
            "source_table": row.get("source_table"),
            "original_record_id": row.get("original_record_id"),
            "raw_disposition": raw,                       # verbatim
            "fleet_disposition": normalize_disposition(raw),
            "subject": {"type": row.get("subject_type"),
                        "id": row.get("subject_id")},
            "observed_at": row.get("observed_at"),
            "updated_at": row.get("updated_at"),
            "is_corrected_upstream": bool(row.get("is_corrected", False)),
            "evidence": {k: v for k, v in row.items() if k in (
                "manufacturer", "reference", "region", "provenance_url",
                "note", "reason", "before_state", "after_state",
                "actor_label") and v is not None},
            "ingestion_snapshot_hash": ingestion_snapshot_hash,
        }
        record["corpus_id"] = _corpus_id(
            clank_id, record["source_table"], record["original_record_id"])
        record["content_hash"] = _content_hash(record)
        block["records"].append(record)
    return block


def build_corpus(previous_batch: dict[str, Any] | None,
                 clank_blocks: dict[str, dict[str, Any]], *,
                 generated_from: str) -> tuple[dict[str, Any], list[str]]:
    """Merge current observations with previous batch into lineage-carrying
    records. Returns (batch_payload, warnings)."""
    warnings: list[str] = []
    prev_index: dict[str, dict[str, Any]] = {}
    if previous_batch:
        for rec in previous_batch.get("corpus", {}).get("records", []):
            prev_index[rec["corpus_id"]] = rec

    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _merge_current(cid: str, recs: list[dict[str, Any]]) -> None:
        for rec in recs:
            old = prev_index.get(rec["corpus_id"])
            if old and old["content_hash"] != rec["content_hash"]:
                rec.update({
                    "supersedes": old["content_hash"],
                    "supersedes_corpus_id": old["corpus_id"],
                    "superseded_raw_disposition": old["raw_disposition"],
                    "correction_detected_in_batch": generated_from,
                })
            elif old:
                rec.setdefault("first_seen_batch", old.get("first_seen_batch"))
                rec["unchanged_since"] = old.get(
                    "unchanged_since", old.get("first_seen_batch"))
            rec.setdefault("first_seen_batch", generated_from)
            seen_ids.add(rec["corpus_id"])
            merged.append(rec)

    for cid in sorted(clank_blocks):
        block = clank_blocks[cid]
        if block.get("observation") == "FAILED_ADAPTER":
            warnings.append(f"{cid}: qc adapter failed ({block.get('error', '')[:100]})")
            prev_block = (previous_batch or {}).get("corpus", {}).get("clanks", {}).get(cid, {})
            _merge_current(cid, prev_block.get("records", []))
            continue
        _merge_current(cid, block.get("records", []))

    # carry forward records for Clanks absent from this run (no deletion)
    for cid_key, old in prev_index.items():
        if cid_key in seen_ids:
            continue
        carried = dict(old)
        carried.setdefault("first_seen_batch", old.get("first_seen_batch", generated_from))
        carried["carried_forward"] = True
        merged.append(carried)

    coverage = {}
    all_cids = ({r["clank_id"] for r in merged}
                | set(clank_blocks.keys())
                | set((previous_batch or {}).get("corpus", {}).get("clanks", {}).keys()))
    for cid in sorted(all_cids):
        recs = [r for r in merged if r["clank_id"] == cid]
        dist: dict[str, int] = {}
        unmapped_examples = []
        for r in recs:
            d = r["fleet_disposition"]
            dist[d] = dist.get(d, 0) + 1
            if d == UNMAPPED and len(unmapped_examples) < 5:
                unmapped_examples.append(r["raw_disposition"])
        eligible_block = clank_blocks.get(cid, {}).get("eligible") or {}
        eligible = eligible_block.get("eligible_total")
        reviewed = len(recs)
        review_rate = round(reviewed / eligible, 4) if isinstance(eligible, int) and eligible else None
        correction_rate = (round(sum(1 for r in recs
                                     if r.get("is_corrected_upstream")
                                     or r.get("supersedes")) / reviewed, 4)
                           if reviewed else None)
        coverage[cid] = {
            "total_records": reviewed,
            "eligible_items": eligible,
            "eligible_detail": {k: v for k, v in eligible_block.items()
                                 if k != "eligible_total"} or None,
            "review_rate": review_rate,
            "disposition_distribution": dist,
            "unmapped_examples": unmapped_examples,
            "unmapped_rate": (round(dist.get(UNMAPPED, 0) / reviewed, 4)
                              if reviewed else None),
            "correction_rate": correction_rate,
            "excluded_machine_scoring": (
                "smartphone confidence_ledger excluded by design"
                if cid == "smartphone-clank" else None)}

    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "derived_label": ("DERIVED QC corpus — Motherclank M4; human decisions "
                          "verbatim + explicit normalization only"),
        "generated_from": generated_from,
        "rules_version": RULES_VERSION,
        "previous_qc_batch_hash": (previous_batch or {}).get("qc_batch_hash"),
        "corpus": {"clanks": clank_blocks, "records": merged},
        "coverage": coverage,
    }
    payload["record_count"] = len(merged)
    payload["qc_batch_hash"] = content_hash(payload)
    return payload, warnings


def content_hash(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "qc_batch_hash"}
    return "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def read_previous_qc_batch(var_dir: Path) -> dict[str, Any] | None:
    d = var_dir / "qc_corpus"
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
            if key >= latest_key:
                latest_key, latest = key, rec
    return latest


def append_qc_batch(out_dir: Path, batch: dict[str, Any]) -> Path:
    day = str(batch["generated_from"])[:10]
    d = out_dir / "qc_corpus"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(batch, sort_keys=True, default=str) + "\n")
    return d / f"{day}.jsonl"


# ---------------------------------------------------------------------------
# Markdown coverage report
# ---------------------------------------------------------------------------

def render_coverage(batch: dict[str, Any]) -> str:
    lines = [
        "# Motherclank QC corpus coverage (DERIVED — M4)",
        "",
        f"- Generated from observations through: {batch.get('generated_from', 'UNKNOWN')}",
        f"- Records: {batch.get('record_count', 0)} · "
        f"Previous batch: {(batch.get('previous_qc_batch_hash') or 'none')[:20]}",
        f"- Corpus hash: {batch.get('qc_batch_hash', 'UNKNOWN')[:20]}…",
        "",
        "| Clank | Records | Fleet distribution | Unmapped examples | Notes |",
        "|---|---|---|---|---|",
    ]
    for cid, cov in sorted((batch.get("coverage") or {}).items()):
        dist = cov.get("disposition_distribution") or {}
        dist_s = ", ".join(f"{k}:{v}" for k, v in sorted(dist.items())) or "none"
        unmapped = ", ".join(repr(u) for u in (cov.get("unmapped_examples") or [])[:3])
        rate = cov.get("review_rate")
        corr = cov.get("correction_rate")
        elig = cov.get("eligible_items")
        extra = []
        if elig is not None:
            extra.append(f"eligible={elig}")
        if rate is not None:
            extra.append(f"review_rate={rate:.1%}")
        if corr is not None:
            extra.append(f"correction_rate={corr:.1%}")
        note_col = "; ".join([e for e in extra if e] + ([cov.get("excluded_machine_scoring")] if cov.get("excluded_machine_scoring") else []))
        lines.append(f"| {cid} | {cov.get('total_records', 0)} | {dist_s} "
                     f"| {unmapped or '-'} | {note_col or '-'} |")
    lines += [
        "",
        "_Raw dispositions are preserved verbatim; fleet values appear only where",
        "the mapping is explicit. Missing feedback is never counted as negative._",
        "",
    ]
    return chr(10).join(lines)
