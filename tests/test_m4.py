"""Motherclank M4 tests — QC corpus ingestion boundaries and lineage."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from motherclank import qc_corpus as qc
from motherclank.adapters import build_adapters


# ---------------------------------------------------------------------------
# Normalization: explicit-only mapping; verbatim preservation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,fleet", [
    ("USEFUL", "USEFUL"),
    ("FALSE_POSITIVE", "FALSE_POSITIVE"),
    ("out_of_stock", "OUT_OF_STOCK"),
    ("not_useful", "NOT_USEFUL"),
    ("Duplicate", "DUPLICATE"),
])
def test_explicit_mappings(raw, fleet):
    assert qc.normalize_disposition(raw) == fleet


@pytest.mark.parametrize("raw", [None, "", "promote", "quarantine",
                                 "helpful-ish", "POSITIVE"])
def test_unmapped_stays_unmapped_with_verbatim_raw(raw):
    assert qc.normalize_disposition(raw) == qc.UNMAPPED
    # raw preserved by ingest path is asserted in record tests below


def test_missing_feedback_is_not_negative():
    """Absent rows produce absent records — never implicit NOT_USEFUL."""
    blocks = {"watch-clank": {"clank_id": "watch-clank", "records": []}}
    payload, _ = qc.build_corpus(None, blocks, generated_from="g1")
    assert payload["record_count"] == 0
    cov = payload["coverage"]["watch-clank"]
    assert cov["fleet_distribution"] == {}
    assert "NOT_USEFUL" not in json.dumps(cov)


import json  # noqa: E402


# ---------------------------------------------------------------------------
# Ingestion via adapters: fixtures with real schemas
# ---------------------------------------------------------------------------

@pytest.fixture()
def qc_state(tmp_path):
    d = tmp_path / "real-state"
    d.mkdir()
    con = sqlite3.connect(d / "watch_clank.db")
    con.executescript("""
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE event_reviews (id INTEGER PRIMARY KEY, event_id INT,
            watch_id INT, manufacturer TEXT, reference_canonical TEXT,
            source_collector_id TEXT, region TEXT, event_type TEXT,
            disposition TEXT, reviewed_at TEXT, evidence_observed_at TEXT,
            availability_status TEXT, provenance_url TEXT, reason TEXT,
            review_metadata TEXT, updated_at TEXT, is_corrected INT);
        CREATE TABLE specialist_lead_reviews (id INTEGER PRIMARY KEY);
        CREATE TABLE collector_runs (id INTEGER PRIMARY KEY, collector_id TEXT,
            status TEXT, started_at TEXT, completed_at TEXT, observation_count INT,
            is_baseline INT);
        """)
    con.execute("INSERT INTO alembic_version VALUES ('0007_test')")
    con.execute("""
        INSERT INTO event_reviews (id, event_id, watch_id, manufacturer,
            reference_canonical, source_collector_id, region, event_type,
            disposition, reviewed_at, updated_at, is_corrected, provenance_url)
        VALUES (1, 124, NULL, 'Timex', 'TW7D18600', NULL, 'US',
                'NEW_REFERENCE', 'FALSE_POSITIVE', '2026-08-19 12:59:45',
                '2026-08-19 13:05:00', 1, 'https://x')
    """)
    con.commit(); con.close()

    con = sqlite3.connect(d / "smartphone_clank.db")
    con.executescript("""
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE confidence_ledger (id TEXT PRIMARY KEY);
        CREATE TABLE analyst_actions (id INTEGER PRIMARY KEY, action TEXT,
            target_type TEXT, target_id TEXT, actor_label TEXT, reason TEXT,
            before_state TEXT, after_state TEXT, created_at TEXT);
        CREATE TABLE collector_run_metrics (id TEXT PRIMARY KEY, collector_name TEXT,
            source_name TEXT, started_at TEXT, finished_at TEXT, duration_ms INT,
            status TEXT, pages_requested INT, pages_fetched INT,
            bytes_downloaded INT, http_requests INT, http_failures INT,
            parser_failures INT, candidates_found INT, valid_devices INT,
            new_devices INT, updated_devices INT, evidence_added INT,
            meaningful_changes INT, alerts_sent INT, maintenance_alerts_sent INT,
            cache_hits INT, cache_misses INT, peak_rss_kb INT, cpu_time_ms INT,
            notes TEXT, meta TEXT, resighted INT, run_reason TEXT);
        """)
    con.execute("INSERT INTO alembic_version VALUES ('0007_x')")
    con.execute("INSERT INTO analyst_actions VALUES (1,'promote_device','device',"
                "'dev-1','operator','looks real','candidate','confirmed','2026-08-22T00:00:00Z')")
    con.commit(); con.close()

    con = sqlite3.connect(d / "korean_tech_wire.db")
    con.executescript("""
        CREATE TABLE schema_migrations (version INT);
        CREATE TABLE article_feedback (id INTEGER PRIMARY KEY, article_id INT,
            outcome TEXT, note TEXT, created_at TEXT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, finished_at TEXT);
        CREATE TABLE source_run_health (id INTEGER PRIMARY KEY, run_id INT, source_id INT,
            attempted_at TEXT, success INT, references_discovered INT, new_articles INT);
        """)
    con.execute("INSERT INTO schema_migrations VALUES (4)")
    con.commit(); con.close()
    return d


def _ingest(real_state: Path, tmp_path: Path, previous=None):
    built = build_adapters(real_state)
    blocks = {cid: qc.ingest_clank(cid, built["adapters"][cid],
                                   ingestion_snapshot_hash="sha256:snap")
              for cid in ("watch-clank", "smartphone-clank", "korean-tech-wire")}
    return qc.build_corpus(previous, blocks,
                           generated_from="g1" if previous is None else "g2")


def snap_build(tmp_path, built, real_state, fleet_yaml):
    raise NotImplementedError


def test_ingest_preserves_verbatim_and_maps_explicitly(qc_state, tmp_path):
    payload, warnings = _ingest(qc_state, tmp_path)
    assert warnings == []
    recs = {(r["clank_id"], r["original_record_id"]): r
            for r in payload["corpus"]["records"]}
    w = recs[("watch-clank", 1)]
    assert w["raw_disposition"] == "FALSE_POSITIVE"          # verbatim
    assert w["fleet_disposition"] == "FALSE_POSITIVE"         # explicit map
    assert w["subject"] == {"type": "event", "id": 124}
    assert w["evidence"]["manufacturer"] == "Timex"
    s = recs[("smartphone-clank", 1)]
    assert s["raw_disposition"] == "promote_device"
    assert s["fleet_disposition"] == "UNMAPPED"               # honest vocabulary gap
    ktw_cov = payload["coverage"]["korean-tech-wire"]
    assert ktw_cov["total_records"] == 0                      # empty feedback ≠ negative


def test_machine_scoring_excluded(qc_state, tmp_path):
    con = sqlite3.connect(qc_state / "smartphone_clank.db")
    con.execute("INSERT INTO confidence_ledger (id) VALUES ('machine-row')")
    con.commit(); con.close()
    payload, _ = _ingest(qc_state, tmp_path)
    smartphone_records = [r for r in payload["corpus"]["records"]
                          if r["clank_id"] == "smartphone-clank"]
    assert all(r["source_table"] == "analyst_actions" for r in smartphone_records)


# ---------------------------------------------------------------------------
# Correction lineage: upstream change => superseding record, history intact
# ---------------------------------------------------------------------------

def test_upstream_correction_creates_superseding_record(qc_state, tmp_path):
    p1, _ = _ingest(qc_state, tmp_path)
    first = [r for r in p1["corpus"]["records"] if r["clank_id"] == "watch-clank"][0]

    # operator corrects the disposition upstream (UPDATE in the Clank's own DB)
    con = sqlite3.connect(qc_state / "watch_clank.db")
    con.execute("UPDATE event_reviews SET disposition='USEFUL', is_corrected=1,"
                "updated_at='2026-08-23T10:00:00' WHERE id=1")
    con.commit(); con.close()

    p2, _ = _ingest(qc_state, tmp_path, previous=p1)
    second = [r for r in p2["corpus"]["records"] if r["clank_id"] == "watch-clank"][0]
    assert second["corpus_id"] == first["corpus_id"]           # same natural key
    assert second["content_hash"] != first["content_hash"]     # changed content
    assert second["supersedes"] == first["content_hash"]
    assert second["superseded_raw_disposition"] == "FALSE_POSITIVE"
    assert second["raw_disposition"] == "USEFUL"
    assert second["is_corrected_upstream"] is True


def test_dedupe_without_deletion_across_batches(qc_state, tmp_path):
    p1, _ = _ingest(qc_state, tmp_path)
    p2, _ = _ingest(qc_state, tmp_path, previous=p1)
    r1 = [r for r in p1["corpus"]["records"] if r["clank_id"] == "watch-clank"][0]
    r2 = [r for r in p2["corpus"]["records"] if r["clank_id"] == "watch-clank"][0]
    assert r1["content_hash"] == r2["content_hash"]
    assert r2.get("supersedes") is None
    assert "supersedes" not in json.dumps(r2) or r2.get("supersedes") is None


# ---------------------------------------------------------------------------
# Read-only proof + no write-back interface
# ---------------------------------------------------------------------------

def test_ingestion_cannot_write_upstream(qc_state, tmp_path):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(qc_state.glob("*.db"))}
    built = build_adapters(qc_state)
    for cid in ("watch-clank", "smartphone-clank", "korean-tech-wire"):
        adapter = built["adapters"][cid]
        adapter.qc_records()
    # direct evidence: ro connections cannot register any change
    for cid in ("watch-clank", "smartphone-clank", "korean-tech-wire"):
        adapter = built["adapters"][cid]
        con = sqlite3.connect(f"file:{adapter.db_path.resolve().as_posix()}?mode=ro", uri=True)
        assert con.total_changes == 0
        con.close()
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(qc_state.glob("*.db"))}
    assert before == after


def test_forbidden_writeback_interfaces_absent_from_qc_module():
    src = Path(__file__).resolve().parents[1] / "src" / "motherclank" / "qc_corpus.py"
    text = src.read_text().lower()
    for forbidden in ("insert into", "update ", "delete from", ".commit(",
                      "executescript", "discord", "requests", "urllib"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# Chaining + coverage report
# ---------------------------------------------------------------------------

def test_qc_batch_chaining(qc_state, tmp_path):
    p1, _ = _ingest(qc_state, tmp_path)
    a1 = qc.append_qc_batch(tmp_path, p1)
    assert a1.exists() and p1["previous_qc_batch_hash"] is None
    prev = qc.read_previous_qc_batch(tmp_path)
    p2, _ = qc.build_corpus(prev, {}, generated_from="g2-empty-safety")
    assert p2["previous_qc_batch_hash"] == p1["qc_batch_hash"]
    # empty safety batch keeps prior records (no deletion)
    assert p2["record_count"] >= p1["record_count"]


def test_coverage_report_renders(qc_state, tmp_path):
    from motherclank.qc_corpus import render_coverage
    payload, _ = _ingest(qc_state, tmp_path)
    text = render_coverage(payload)
    assert "| watch-clank |" in text and "FALSE_POSITIVE" in text
    assert "never counted as negative" in text
