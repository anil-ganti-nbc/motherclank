"""P-4.4.1 goldens — SI evidence_envelopes end-to-end through the
PRODUCTION observer dispatch path, not direct method calls.

GIC-40/41 formally registered in golden_corpus.py.
Zero-claims behavior: clean empty list.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import snapshot as snap
from motherclank import synthesis as syn


def _si_realistic_db(tmp_path: Path):
    """Realistic SI fixture from canonical participant schema."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "semiconductor_intelligence.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE provider_runs (id INTEGER PRIMARY KEY,"
                " provider TEXT, source_id INTEGER, started_at TEXT,"
                " finished_at TEXT, items_collected INTEGER,"
                " duplicates_skipped INTEGER, status TEXT, error TEXT)")
    con.execute("CREATE TABLE operational_job_runs (id INTEGER PRIMARY KEY,"
                " job_type TEXT, trigger_type TEXT, started_at TEXT,"
                " finished_at TEXT, status TEXT, attempt_number INTEGER"
                " DEFAULT 1, parent_retry_id INTEGER, owner_identity TEXT"
                " DEFAULT '', lock_token TEXT, summary TEXT DEFAULT '',"
                " result_counts TEXT DEFAULT '{}', error_summary TEXT,"
                " next_retry_at TEXT, diagnostic_reference TEXT,"
                " created_at TEXT)")
    con.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY,"
                " statement TEXT, subject_entity_id INTEGER,"
                " status TEXT, confidence REAL,"
                " resolution_note TEXT, created_at TEXT,"
                " updated_at TEXT, resolved_at TEXT)")
    con.execute("CREATE TABLE claim_events (id INTEGER PRIMARY KEY,"
                " claim_id INTEGER, event_type TEXT,"
                " confidence_after REAL, note TEXT, created_at TEXT)")
    con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY,"
                " polling_enabled INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32)"
                " PRIMARY KEY)")

    # one operational job run (scheduler-triggered)
    con.execute("INSERT INTO operational_job_runs (id, job_type,"
                " trigger_type, started_at, finished_at, status,"
                " attempt_number, error_summary) VALUES"
                " (1,'pipeline','scheduler','2026-08-27T05:00:00Z',"
                " '2026-08-27T05:10:00Z','successful',1,NULL)")

    # manual-only source (polling disabled)
    con.execute("INSERT INTO sources VALUES (1, 0)")

    # zero provider_runs — legitimate for manual-only config

    # one Claim with lifecycle
    con.execute("INSERT INTO claims VALUES (1,"
                " 'TSMC 2nm yields above 60%', NULL, 'confirmed', 0.82,"
                " NULL, '2026-08-01T00:00:00Z', '2026-08-20T00:00:00Z',"
                " NULL)")
    con.execute("INSERT INTO claim_events VALUES (1, 1, 'created', 0.5,"
                " 'initial', '2026-08-01T00:00:00Z')")
    con.execute("INSERT INTO claim_events VALUES (2, 1,"
                " 'confidence_updated', 0.82, 'evidence linked',"
                " '2026-08-20T00:00:00Z')")

    con.execute("INSERT INTO alembic_version VALUES ('abc123')")
    con.commit()
    con.close()
    return db


def _build_snapshot(tmp_path: Path):
    db = _si_realistic_db(tmp_path)
    entry = adapters_mod.BUILTIN_REGISTRY["semiconductor-intelligence"]
    module = __import__(entry["module"], fromlist=[entry["class"]])
    adapter = getattr(module, entry["class"])(db_path=db)
    built = {"adapters": {"semiconductor-intelligence": adapter},
             "versions": {"adapter_contract_version": "t"}, "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(
        inventory_path=inv, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    payload["harvested_at_utc"] = "2026-08-27T12:00:00Z"
    payload["content_hash"] = "sha256:si-e2e"
    return payload, warnings, adapter


def test_si_evidence_envelopes_through_production_harvest(tmp_path):
    """GIC-41 lock: intelligence_assertion@1 flows through the ACTUAL
    production observer path (observe_clank → generic extension dispatch →
    evidence_envelopes), not a direct method call."""
    payload, warnings, adapter = _build_snapshot(tmp_path)

    # evidence_envelopes was invoked by the generic dispatcher
    block = payload["clanks"]["semiconductor-intelligence"]
    envs = block.get("evidence_envelopes")
    assert envs is not None, f"evidence_envelopes missing; block keys: {sorted(block.keys())}"
    assert len(envs) >= 1, f"empty envelopes; type={type(envs).__name__}; keys={sorted(envs.keys()) if isinstance(envs, dict) else 'n/a'}"
    if isinstance(envs, dict):
        print("DEBUG: envs is dict with keys:", sorted(envs.keys()), file=__import__('sys').stderr)
        envs_list = list(envs.values())
    else:
        envs_list = envs
    env = envs_list[0]
    assert env["evidence_type"] == "intelligence_assertion"
    assert env["evidence_version"] == 1
    assert env["subject"]["clank_id"] == "semiconductor-intelligence"
    assert env["payload"]["assertion_ref"] == "claims/1"
    assert env["payload"]["native_confidence"] == 0.82
    assert env["content_hash"].startswith("sha256:")


def test_si_evidence_classification_and_consumer(tmp_path):
    payload, _, _ = _build_snapshot(tmp_path)
    from motherclank import evidence as ev_mod
    block = payload["clanks"]["semiconductor-intelligence"]
    envs = block["evidence_envelopes"]

    cls, violations = ev_mod.classify(envs[0])
    assert cls == "KNOWN" and violations == []

    out = ev_mod.consume_all(envs)
    dc = out["derived_claims"][0]
    summary = dc["claims"]["assertion_summary"]
    assert summary["by_status"] == {"confirmed": 1}
    assert summary["native_confidence"] == 0.82
    assert "NOT an observer truth judgment" in summary["note"]


def test_si_synthesis_with_evidence_derivation(tmp_path):
    from motherclank import synthesis as syn
    payload, _, _ = _build_snapshot(tmp_path)
    synth = syn.synthesize_fleet(payload)
    claim = synth["clanks"]["semiconductor-intelligence"]
    assert claim["capability_states"]["collection"]["state"] == "active"


def test_zero_claims_emits_empty_envelopes_cleanly(tmp_path):
    """Live SI has zero claims. The production observer must handle this:
    evidence_envelopes() -> [], no error, no fabricated assertion."""
    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY,"
                " statement TEXT, status TEXT, confidence REAL,"
                " created_at TEXT, updated_at TEXT)")
    con.commit()
    con.close()

    adapter = SemiconductorIntelligenceAdapter(db_path=db)
    envelopes = adapter.evidence_envelopes()
    assert envelopes == []

    built = {"adapters": {"semiconductor-intelligence": adapter},
             "versions": {}, "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(
        inventory_path=inv, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    block = payload["clanks"]["semiconductor-intelligence"]
    envs = block.get("evidence_envelopes", [])
    assert envs == []


def test_gic40_formal_entry_exists_and_passes():
    from motherclank.golden_corpus import get, ids
    e = get("GIC-40")
    assert e is not None
    assert e["status"] == "executable"
    assert any("provider_runs=0" in f for f in e["forbidden"])
    assert "GIC-40" in ids()


def test_gic41_formal_entry_exists_and_passes():
    from motherclank.golden_corpus import get, ids
    e = get("GIC-41")
    assert e is not None
    assert e["status"] == "executable"
    assert "hardcoded extension invocation lists" in json.dumps(e["forbidden"])
    assert "GIC-41" in ids()


def test_corpus_count_is_exactly_41():
    from motherclank.golden_corpus import ids
    assert len(ids()) == 41
