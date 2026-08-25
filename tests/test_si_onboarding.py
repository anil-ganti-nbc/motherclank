"""Semiconductor Intelligence v0.3 extension dogfood - claims-and-evidence
subject model, generic intelligence_assertion typed evidence, ACT-003
honesty, participant-native confidence preservation.

Fixtures built strictly from schema evidence in canonical SI source
(semi_intel/domain/models.py: claims / claim_evidence_links / claim_events /
provider_runs / sources; alembic versioning).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import evidence as ev
from motherclank.evidence import register_consumer_for_type
from motherclank import snapshot as snap
from motherclank import synthesis as syn

NOW = "2026-08-27T12:00:00Z"


def _si_fixture(tmp_path: Path, *, zero_items=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "semiconductor_intelligence.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE provider_runs (id INTEGER PRIMARY KEY,"
                " provider TEXT, source_id INTEGER, started_at TEXT,"
                " finished_at TEXT, items_collected INTEGER,"
                " duplicates_skipped INTEGER, status TEXT, error TEXT)")
    con.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY,"
                " statement TEXT, status TEXT, confidence REAL,"
                " created_at TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32)"
                " PRIMARY KEY)")
    items = 0 if zero_items else 14
    con.execute("INSERT INTO provider_runs VALUES (1,'rss',NULL,"
                "'2026-08-26T05:00:00Z','2026-08-26T05:10:00Z',?,2,'ok',"
                "NULL)", (items,))
    con.execute("INSERT INTO provider_runs VALUES (2,'x',NULL,"
                "'2026-08-26T06:00:00Z','2026-08-26T06:05:00Z',3,1,'ok',"
                "NULL)")
    con.execute("INSERT INTO claims VALUES (1,'TSMC 2nm yields above 60%',"
                "'confirmed',0.82,'2026-08-01T00:00:00Z',"
                "'2026-08-20T00:00:00Z')")
    con.execute("INSERT INTO alembic_version VALUES ('head1')")
    con.commit()
    con.close()
    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)
    return SemiconductorIntelligenceAdapter(db_path=db)


def _assertion_envelope(**kw):
    base = dict(
        evidence_type="intelligence_assertion", evidence_version=1,
        subject={"clank_id": "semiconductor-intelligence"},
        observed_at=NOW,
        substrate="sqlite:claims",
        payload={"assertion_ref": "claims/1",
                 "status": "confirmed",
                 "native_confidence": 0.82,
                 "occurred_at": "2026-08-20T00:00:00Z"},
        provenance={"query": "SELECT ... FROM claims",
                    "participant_table": "claims"})
    base.update(kw)
    return ev.make_envelope(**base)


# ---------------------------------------------------------------------------
# Execution substrate: native passes; zero-collection is legitimate
# ---------------------------------------------------------------------------

def test_si_provider_pass_zero_collected_is_ok_not_failure(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    a = _si_fixture(tmp_path / "zero", zero_items=True)
    health = a.health()
    rss = next(s for s in health.sources if s.source_id == "rss")
    assert rss.status.value == "ok"
    assert rss.observed_count == 0          # maintenance/no-work pass


def test_si_last_run_is_native_with_clock_label(tmp_path):
    a = _si_fixture(tmp_path)
    lr = a.last_run()
    assert lr["supported"] is True
    assert lr["clock"] == "native_run_row"  # genuinely native run table
    assert lr["duplicates_skipped"] == 1


def test_si_schema_revision_via_alembic(tmp_path):
    a = _si_fixture(tmp_path)
    assert a.schema_revision() == "head1"
    missing = type(a)(db_path=tmp_path / "missing.db")
    assert missing.schema_revision() is None


# ---------------------------------------------------------------------------
# Typed evidence: intelligence_assertion@1 (generic fleet-wide extension)
# ---------------------------------------------------------------------------

def test_intelligence_assertion_classifies_known():
    cls, violations = ev.classify(_assertion_envelope())
    assert (cls, violations) == ("KNOWN", [])


def test_intelligence_assertion_payload_validation():
    bad = _assertion_envelope(payload={"native_confidence": 0.5})
    cls, violations = ev.classify(bad)
    assert cls == "KNOWN_PAYLOAD_INVALID"
    assert any("assertion_ref" in x for x in violations)


def test_native_confidence_preserved_verbatim_never_normalized():
    out = ev.consume_all([_assertion_envelope()])
    claim = out["derived_claims"][0]
    summary = claim["claims"]["assertion_summary"]
    assert summary["native_confidence"] == 0.82   # preserved verbatim
    assert summary["by_status"] == {"confirmed": 1}


def test_gic39_participant_confidence_is_not_observer_truth():
    """New corpus entry: a participant-native 0.92 confidence must never be
    reinterpreted as Motherclank certainty."""
    env = _assertion_envelope(payload={"assertion_ref": "c/9",
                                       "status": "debunked",
                                       "native_confidence": 0.92,
                                       "occurred_at": NOW})
    out = ev.consume_all([env])
    summary = out["derived_claims"][0]["claims"]["assertion_summary"]
    # debunked stays debunked even at native confidence 0.92:
    assert summary["by_status"] == {"debunked": 1}
    assert "NOT an observer truth judgment" in summary["note"]


# ---------------------------------------------------------------------------
# Registry/hot-swap/core ignorance + read-only proofs
# ---------------------------------------------------------------------------

def test_si_registered_without_core_participant_logic():
    entry = adapters_mod.BUILTIN_REGISTRY["semiconductor-intelligence"]
    assert entry["class"] == "SemiconductorIntelligenceAdapter"
    core = Path(snap.__file__).parent
    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "qc_corpus",
              "soak", "drift", "report", "inbox_bridge", "registry_shim",
              "cli", "contract"):
        code = (core / f"{m}.py").read_text(encoding="utf-8")
        low = code.lower()
        assert "semiconductor" not in low and "si-" not in low, \
            f"{m}.py hardcodes SI"
        assert "intelligence_assertion" not in code or m == "evidence", \
            f"{m}.py embeds assertion vocabulary outside the evidence plane"


def test_capability_states_canonical_and_honest(tmp_path):
    a = _si_fixture(tmp_path)
    from clank_runtime.contracts.capabilities import \
        validate_capability_states
    cs = a.capability_states()
    assert validate_capability_states(cs) == []
    assert cs["scheduler_trace"]["state"] in CANONICAL_STATES
    assert "ACT-003" in cs["scheduler_trace"]["evidence"]
    assert cs["continuity"]["state"] == "unknown_or_unverified"  # unproven


CANONICAL_STATES = {"active", "supported_unconfigured",
                    "supported_undeployed", "unsupported_by_policy",
                    "unsupported", "unknown_or_unverified"}


def test_read_only_missing_store_never_created(tmp_path):
    missing = tmp_path / "never.db"
    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)
    adapter = SemiconductorIntelligenceAdapter(db_path=missing)
    snap.observe_clank(adapter)
    assert not missing.exists()


def test_read_only_existing_store_byte_identical(tmp_path):
    a = _si_fixture(tmp_path)
    before = hashlib.sha256(a.db_path.read_bytes()).hexdigest()
    snap.observe_clank(a)
    after = hashlib.sha256(a.db_path.read_bytes()).hexdigest()
    assert before == after


def test_sibling_isolation_broken_si_never_poisons_ctw(tmp_path):
    broken = _make_si_raising(tmp_path)
    healthy = {"clank_version": "1",
               "status": {"operational_state": "healthy"},
               "health": {"sources": [{"source_id": "s", "status": "ok"}]},
               "last_run": {"finished_at": NOW}}
    built = {"adapters": {"semiconductor-intelligence": broken,
                          "chinese-tech-wire": HealthyStub(
                              db_path=tmp_path / "ctw.db")},
             "versions": {}, "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(inventory_path=inv,
                                            adapters_result=built,
                                            real_state_dir=tmp_path,
                                            out_dir=tmp_path)
    blocks = payload["clanks"]
    assert blocks["semiconductor-intelligence"].get(
        "observation") == "FAILED_ADAPTER"
    assert blocks["chinese-tech-wire"].get("observation") != "FAILED_ADAPTER"
    assert warnings


class HealthyStub:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    def identity(self):
        class D:
            clank_version = "1"
            contract_version = "0.1.0-v3"
        return D()

    def capabilities(self):
        class C:
            supports_delivery_accounting = False
        return C()

    def status(self):
        return {"operational_state": "healthy"}

    def health(self):
        return {"sources": [{"source_id": "s", "status": "ok"}]}

    def last_run(self):
        return {"supported": False}

    def capability_states(self):
        return {"collection": {"state": "unknown_or_unverified",
                               "evidence": "stub"}}


def _make_si_raising(tmp_path):
    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)

    class Raising(SemiconductorIntelligenceAdapter):
        def identity(self):
            raise RuntimeError("identity exploded")

    return Raising(db_path=tmp_path / "si-broken.db")
