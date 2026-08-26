"""P-4.4 goldens — dual execution planes, generic extension dispatch,
evidence_envelopes reachability, core-ignorance for SI substrate tokens.

GIC-40: application execution present, provider plane empty → no gap.
GIC-41: declared evidence producer unreachable by observer dispatch.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import contract as obs_contract
from motherclank import snapshot as snap
from motherclank.contract import (
    optional_extension_names,
    register_optional_extension,
)


# ---------------------------------------------------------------------------
# GIC-40: operational_job_runs present, provider_runs empty
# ---------------------------------------------------------------------------

def _si_db(tmp_path: Path, *, with_provider=False):
    db = tmp_path / "si.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE operational_job_runs (id INTEGER PRIMARY KEY,"
        " job_type TEXT, trigger_type TEXT, started_at TEXT,"
        " finished_at TEXT, status TEXT, attempt_number INTEGER DEFAULT 1,"
        " error_summary TEXT)")
    con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY,"
                " polling_enabled INTEGER DEFAULT 0)")
    if with_provider:
        con.execute("CREATE TABLE provider_runs (id INTEGER PRIMARY KEY,"
                    " provider TEXT, source_id INTEGER, started_at TEXT,"
                    " finished_at TEXT, items_collected INTEGER DEFAULT 0,"
                    " duplicates_skipped INTEGER DEFAULT 0, status TEXT,"
                    " error TEXT)")
    con.execute("INSERT INTO sources VALUES (1, 0)")  # manual-only
    con.execute(
        "INSERT INTO operational_job_runs VALUES "
        "(1,'pipeline','scheduler','2026-08-27T05:00:00Z',"
        "'2026-08-27T05:10:00Z','successful',1,NULL)")
    if with_provider:
        con.execute(
            "INSERT INTO provider_runs VALUES (1,'rss',1,"
            "'2026-08-26T05:00:00Z','2026-08-26T05:01:00Z',14,2,'ok',NULL)")
    con.commit()
    con.close()
    return db


def _adapter(db):
    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)
    return SemiconductorIntelligenceAdapter(db_path=db)


def test_gic40_app_execution_present_provider_empty_is_honest(tmp_path):
    """Application executed successfully; provider plane empty by design."""
    a = _adapter(_si_db(tmp_path))
    health = a.health()
    assert health.overall_status.value == "healthy"
    assert len(health.sources) == 0  # no provider entries — honest

    lr = a.last_run()
    assert lr["supported"] is True
    assert lr["clock"] == "native_operational_job_run"
    assert lr["substrate"] == "operational_job_runs"
    assert lr["trigger_type"] == "scheduler"
    assert "provider_plane" in lr

    pcs = a.provider_collection_summary()
    assert pcs["available"] is True
    assert pcs["latest_activity"] is None
    assert pcs["sources"]["polling_enabled"] == 0


def test_gic40b_with_provider_runs_both_planes_present(tmp_path):
    a = _adapter(_si_db(tmp_path, with_provider=True))
    lr = a.last_run()
    # job plane is primary (fresher or not — substrate is deterministic)
    assert lr["clock"] == "native_operational_job_run"
    pp = a.provider_collection_summary()
    assert pp["runs_present"] is True


# ---------------------------------------------------------------------------
# Generic extension dispatch (replaces hardcoded list)
# ---------------------------------------------------------------------------

class ExtensionAdapter:
    """Synthetic adapter implementing a newly registered extension."""

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
        return {"operational_state": "unknown"}

    def health(self):
        return {"sources": []}

    def last_run(self):
        return {"supported": False}

    def capability_states(self):
        return {}

    def brand_census_v2(self):
        return {"brands": ["citizen", "seiko", "orient"], "total": 3}


def test_generic_dispatch_invokes_newly_registered_extension(tmp_path):
    """Adding a new optional extension must NOT require editing snapshot.py.
    Register it via the public API and prove it's invoked generically."""
    register_optional_extension(
        "brand_census_v2", since="0.3.1",
        description="test extension for dispatch proof")
    adapter = ExtensionAdapter()
    block = snap.observe_clank(adapter)
    assert block.get("brand_census_v2") == {
        "brands": ["citizen", "seiko", "orient"], "total": 3}
    # clean up so other tests don't see it
    from motherclank import contract as cmod
    cmod._OPTIONAL_EXTENSIONS.pop("brand_census_v2", None)


def test_undeclared_method_never_invoked(tmp_path):
    class Sneaky(ExtensionAdapter):
        def stealth_data(self):
            return {"secret": True}

    block = snap.observe_clank(Sneaky())
    assert "stealth_data" not in block


def test_raising_extension_isolated_per_key(tmp_path):
    class ExplodingExt(ExtensionAdapter):
        def boom_extension(self):
            raise RuntimeError("extension bug")

    register_optional_extension(
        "boom_extension", since="0.3.1", description="test")
    try:
        block = snap.observe_clank(ExplodingExt())
        entry = block.get("boom_extension")
        assert isinstance(entry, dict)
        assert entry.get("observation") == "FAILED_ADAPTER"
        assert "extension bug" in entry.get("error", "")
        # sibling extensions still probed
        assert "capability_states" in block
    finally:
        from motherclank import contract as cmod
        cmod._OPTIONAL_EXTENSIONS.pop("boom_extension", None)


def test_extension_registry_is_deterministic():
    names = optional_extension_names()
    assert list(names) == sorted(names)
    assert "capability_states" in names
    assert "evidence_envelopes" in names


def test_core_has_no_hardcoded_extension_list():
    """Regression: snapshot.py must not contain the old hardcoded tuple."""
    src = Path(snap.__file__).read_text(encoding="utf-8")
    code_only = "\n".join(
        l for l in src.splitlines() if not l.strip().startswith("#"))
    assert 'for extra in ("event_summary"' not in code_only


# ---------------------------------------------------------------------------
# GIC-41: declared evidence producer reachable end-to-end via harvest path
# ---------------------------------------------------------------------------

class EvidenceProducingAdapter(ExtensionAdapter):
    def evidence_envelopes(self):
        return [{
            "evidence_spec": 1,
            "evidence_type": "intelligence_assertion",
            "evidence_version": 1,
            "subject": {"clank_id": "test-si"},
            "observed_at": "2026-08-27T06:00:00Z",
            "occurred_at": None,
            "substrate": "sqlite:claims",
            "payload": {
                "assertion_ref": "claims/1",
                "status": "confirmed",
                "native_confidence": 0.82,
                "occurred_at": "2026-08-20T00:00:00Z"},
            "provenance": {"query": "SELECT * FROM claims LIMIT 1"},
            "content_hash": "sha256:test",
        }]


def test_gic41_evidence_envelopes_flow_through_full_harvest(tmp_path):
    from motherclank import evidence as ev_mod
    from motherclank.evidence import consume_all, classify
    adapter = EvidenceProducingAdapter()
    envelopes = adapter.evidence_envelopes()
    assert len(envelopes) == 1
    cls, violations = ev_mod.classify(envelopes[0])
    assert cls == "KNOWN"
    assert violations == []
    out = ev_mod.consume_all(envelopes)
    assert len(out["derived_claims"]) == 1
    dc = out["derived_claims"][0]
    assert dc["claims"]["assertion_summary"]["native_confidence"] == 0.82


def test_gic41_conformance_reachability_via_snapshot(tmp_path):
    """The production observer dispatch path must reach evidence_envelopes
    without any snapshot.py edit. This is the GIC-41 regression lock."""
    adapter = EvidenceProducingAdapter()
    block = snap.observe_clank(adapter)
    envs = block.get("evidence_envelopes")
    assert envs is not None and len(envs) >= 1
    payload = envs[0].get("payload") or {}
    assert payload.get("assertion_ref") == "claims/1"


# ---------------------------------------------------------------------------
# Core ignorance for SI-specific substrate tokens
# ---------------------------------------------------------------------------

def test_core_ignorance_no_si_substrate_tokens():
    import re
    core = Path(snap.__file__).parent
    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "qc_corpus",
              "soak", "drift", "report", "inbox_bridge", "registry_shim",
              "cli", "contract", "scheduler_traces"):
        text = (core / f"{m}.py").read_text(encoding="utf-8")
        code_only = "\n".join(
            re.sub(r'"""[\s\S]*?"""', "", l) for l in text.splitlines())
        code_only = "\n".join(
            re.sub(r"#.*$", "", l) for l in code_only.splitlines())
        for token in ("semiconductor", "semintel", "operational_job_runs",
                      "provider_runs", "chinese-tech-wire"):
            assert token.lower() not in code_only.lower(), \
                f"{m}.py contains participant token '{token}'"
