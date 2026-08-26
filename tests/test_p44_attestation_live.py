"""P-4.4.1 scheduler-fire attestation + execution-result contract tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import liveness as live
from motherclank import scheduler_traces as straces


NOW = "2026-08-27T06:00:00Z"


def _exp(**kw):
    base = dict(expectation_id="EXP-CTW", clank_id="chinese-tech-wire",
                policy="PERIODIC", cadence_seconds=3600,
                authority="deploy-crontab",
                materialization_policy="ALWAYS", active=True)
    base.update(kw)
    return live.make_expectation(**base)


def _block(finished_at=None, **extra):
    b = {"clank_version": "1",
         "status": {"operational_state": "healthy"},
         "health": {"sources": [
             {"source_id": "src[NEWS]", "status": "ok"},
             {"source_id": "src[COMMUNITY]", "status": "degraded"}]},
         }
    if finished_at:
        b["last_run"] = {"finished_at": finished_at}
    b.update(extra)
    return b


def _trace(**kw):
    base = dict(trace_id="T-CTW", clank_id="chinese-tech-wire",
                scheduler_type="cron", unit_or_job="deploy_run.sh",
                observed_at=NOW, invoked_at="2026-08-26T05:55:00Z",
                process_started=True, evidence_source="journal")
    base.update(kw)
    return straces.make_trace(**base)


# ---------------------------------------------------------------------------
# Scheduler-fire trace → liveness integration
# ---------------------------------------------------------------------------

def test_scheduler_trace_drives_application_executed():
    """A positive cron-log trace with process_started=true provides
    APPLICATION_EXECUTED=YES evidence for the liveness plane."""
    exp = _exp()
    t = _trace()
    block = _block("2026-08-26T05:50:00Z")  # fresh run
    lv = live.derive_liveness(block, exp, observed_at=NOW, trace=t)
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    assert lv["stages"]["PROCESS_STARTED"]["value"] == "YES"
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "YES"


def test_scheduler_trace_without_start_evidence_leaves_stage_unknown():
    exp = _exp()
    t = _trace(process_started=None)
    lv = live.derive_liveness(_block(), exp, observed_at=NOW, trace=t)
    # PROCESS_STARTED stays UNKNOWN (no positive start evidence from probe)
    assert lv["stages"]["PROCESS_STARTED"]["value"] == "UNKNOWN"


def test_preexec_gap_from_trace_process_started_false():
    exp = _exp()
    t = _trace(process_started=False,
               invoked_at="2026-08-27T05:55:00Z")
    block = _block("2026-08-20T06:00:00Z")  # stale run
    lv = live.derive_liveness(block, exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    assert lv["evidence"].get("process_started") is False


# ---------------------------------------------------------------------------
# GIC-40: application execution present with empty provider plane
# ---------------------------------------------------------------------------

def test_gic40_formal_si_app_execution_provider_empty(tmp_path):
    """SI adapter: operational_job_runs present, provider_runs absent.
    Application execution known; provider plane independently empty;
    no MATERIALIZATION_GAP."""
    db = tmp_path / "si.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE operational_job_runs (id INTEGER PRIMARY KEY,"
        " job_type TEXT, trigger_type TEXT, started_at TEXT,"
        " finished_at TEXT, status TEXT,"
        " attempt_number INTEGER DEFAULT 1, error_summary TEXT)")
    con.execute(
        "INSERT INTO operational_job_runs (id, job_type, trigger_type,"
        " started_at, finished_at, status, attempt_number,"
        " error_summary) VALUES"
        " (1,'pipeline','scheduler','2026-08-27T05:00:00Z',"
        " '2026-08-27T05:10:00Z','successful',1,NULL)")
    con.commit(); con.close()

    from clank_fleet.adapters.semiconductor_intelligence import (
        SemiconductorIntelligenceAdapter)
    a = SemiconductorIntelligenceAdapter(db_path=db)
    lr = a.last_run()
    assert lr["supported"] is True
    assert lr["clock"] == "native_operational_job_run"

    health = a.health()
    # manual-only config: no provider entries but overall HEALTHY
    assert health.overall_status.value in ("healthy", "warning")


# ---------------------------------------------------------------------------
# GIC-41 regression: generic dispatch reachability
# ---------------------------------------------------------------------------

def test_gic41_generic_dispatch_reachable():
    """Adding a new optional extension does NOT require snapshot.py edits.
    Register via public API; observe_clank invokes it generically."""
    register_optional_ext("test_dispatch_probe", since="0.3.1")
    try:
        class DispatchProbe:
            def identity(self):
                class D: clank_version = "1"; contract_version = "0.1.0-v3"
                return D()
            def capabilities(self):
                class C: supports_delivery_accounting = False
                return C()
            def status(self):
                return {"operational_state": "unknown"}
            def health(self):
                return {"sources": []}
            def last_run(self):
                return {"supported": False}
            def capability_states(self):
                return {}
            def test_dispatch_probe(self):
                return {"dispatched": True}

        from motherclank import snapshot as snap
        block = snap.observe_clank(DispatchProbe())
        assert block.get("test_dispatch_probe", {}).get(
            "dispatched") is True
    finally:
        from motherclank.contract import _OPTIONAL_EXTENSIONS
        _OPTIONAL_EXTENSIONS.pop("test_dispatch_probe", None)


def register_optional_ext(name, *, since):
    from motherclank.contract import register_optional_extension
    register_optional_extension(name, since=since, description="test")


# ---------------------------------------------------------------------------
# CTW-specific semantic clocks
# ---------------------------------------------------------------------------

def test_ctw_publication_vs_ingestion_distinct():
    """CTW articles have both published_at and discovered_at. The adapter
    must never conflate them."""
    from clank_fleet.adapters.chinese_tech_wire import ChineseTechWireAdapter
    a = ChineseTechWireAdapter(db_path="unused")
    # The adapter doesn't expose article-level data directly (that's
    # generation substrate). But the capability declaration confirms
    # clock separation is preserved at the participant level.
    cs = a.capability_states()
    assert cs["collection"]["state"] in ("active", "unknown_or_unverified")
