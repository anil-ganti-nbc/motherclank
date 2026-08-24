"""P-4.1 goldens - no-work execution semantics + materialization policy.

G1 positive non-fire        -> scheduler result without fabrication
G2 fired / not started      -> MATERIALIZATION_GAP (pre-exec)
G3 mandatory materializer   -> persistence-failure GAP past bound
G4 successful no-work       -> NO_WORK_DUE, never a gap
G5 no no-work evidence      -> UNKNOWN, not assumed success
G6 failed participant run   -> application failure, not gap
G7 multi-cadence no-work    -> evidence retained, no invented timing bound

Plus the OEM real production shape (trace-backed) at the synthesis seam.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import anomalies as ano
from motherclank import liveness as live
from motherclank import scheduler_traces as straces
from motherclank import snapshot as snap
from motherclank import synthesis as syn


def _exp(**kw):
    base = dict(expectation_id="EXP", clank_id="c", policy="PERIODIC",
                cadence_seconds=3600, authority="cron", active=True)
    base.update(kw)
    return live.make_expectation(**base)


def _block(finished_at=None, healthy=True):
    b = {"clank_version": "1",
         "status": {"operational_state": "healthy" if healthy else "degraded"},
         "health": {"sources": [{"source_id": "s", "status": "ok"}]}}
    if finished_at:
        b["last_run"] = {"finished_at": finished_at}
    return b


def _trace(**kw):
    base = dict(trace_id="T", clank_id="c", scheduler_type="cron",
                observed_at="2026-08-25T06:00:00Z",
                invoked_at="2026-08-25T05:55:00Z", process_started=True,
                evidence_source="journal")
    base.update(kw)
    return straces.make_trace(**base)


NOW = "2026-08-25T06:00:00Z"

# ---------------------------------------------------------------------------
# Unit level
# ---------------------------------------------------------------------------

def test_g1_positive_non_fire_is_not_invented_into_a_fire():
    exp = _exp()
    t = _trace(invoked_at=None, process_started=None,
               notes="probe positively observed cron window with no entry")
    lv = live.derive_liveness(_block(), exp, observed_at=NOW, trace=t)
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "UNKNOWN"
    assert lv["liveness_state"] == "UNKNOWN"


def test_g2_fired_not_started_is_preecec_gap():
    exp = _exp(materialization_policy="ALWAYS")
    t = _trace(process_started=False)
    lv = live.derive_liveness(_block("2026-08-20T06:00:00Z"), exp,
                              observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "NO"


def test_g3_mandatory_materializer_missing_record_is_gap():
    exp = _exp(materialization_policy="ALWAYS")
    t = _trace(execution_result="completed")
    stale_run = _block("2026-08-20T06:00:00Z")
    lv = live.derive_liveness(stale_run, exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    assert lv["evidence"]["materialization_policy"] == "ALWAYS"


def test_g4_successful_no_work_is_never_a_gap():
    exp = _exp(materialization_policy="WHEN_WORK_ATTEMPTED")
    t = _trace(execution_result="no_work_due",
               execution_detail="done: 0 source(s) crawled")
    lv = live.derive_liveness(_block(), exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "NO_WORK_DUE"
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "YES"
    assert lv["evidence"]["participant_record"] == "none - intentional"


def test_g5_no_nowork_evidence_stays_unknown_for_optional_materializers():
    exp = _exp(materialization_policy="UNKNOWN")
    t = _trace(execution_result=None)
    lv = live.derive_liveness(_block("2026-08-20T06:00:00Z"), exp,
                              observed_at=NOW, trace=t)
    assert lv["liveness_state"] != "MATERIALIZATION_GAP"  # cannot know
    assert lv["liveness_state"] in ("EXECUTION_STALE", "UNKNOWN")


def test_g6_failed_participant_run_is_application_failure():
    exp = _exp(materialization_policy="ALWAYS")
    t = _trace(execution_result="failed")
    block = _block("2026-08-24T05:56:00Z")  # fresh failed run materialized
    block["last_run"]["status"] = "failed"
    block["status"] = {"operational_state": "degraded"}
    lv = live.derive_liveness(block, exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] != "MATERIALIZATION_GAP"  # it materialized


def test_g7_multi_cadence_no_work_keeps_evidence_without_timing_bound():
    exp = _exp(clank_id="mc", cadence_seconds=None, multi_cadence=True)
    t = _trace(execution_result="no_work_due")
    lv = live.derive_liveness(_block(), exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "NO_WORK_DUE"
    assert "requires a declared cadence" not in json.dumps(lv)


def test_trace_schema_rejects_unknown_execution_results():
    with pytest.raises(ValueError):
        _trace(execution_result="sort_of_worked")


def test_expectation_schema_rejects_bad_materialization_policy():
    with pytest.raises(ValueError):
        _exp(materialization_policy="SOMETIMES")


# ---------------------------------------------------------------------------
# Seam level: OEM real production shape through synthesis + detect
# ---------------------------------------------------------------------------

def _oem_real_shape_fixture(tmp_path: Path):
    """Zero crawler_runs rows for the recent executions (the live shape):
    the application ran, was due-gated to nothing, wrote no run record."""
    from clank_fleet.adapters.oem_radar import OemRadarAdapter
    db = tmp_path / "oem_radar.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE crawler_runs (id INTEGER PRIMARY KEY, source_key TEXT,"
        " status TEXT, started_at TEXT, finished_at TEXT, stats_json TEXT)")
    # last persisted work is old; recent executions were intentional no-work
    con.execute("INSERT INTO crawler_runs VALUES (1,'dell_us','ok',"
                "'2026-08-24T05:00:00Z','2026-08-24T05:01:00Z','{}')")
    con.commit()
    con.close()
    adapter = OemRadarAdapter(db_path=db)
    built = {"adapters": {"oem-radar": adapter},
             "versions": {"adapter_contract_version": "t"},
             "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, _ = snap.build_snapshot(
        inventory_path=inv, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    payload["harvested_at_utc"] = NOW
    payload["content_hash"] = "sha256:oem-real-shape"
    return payload


def test_oem_real_shape_synthesis_never_fabricates_gap(tmp_path):
    exp = live.make_expectation(
        expectation_id="EXP-OEM", clank_id="oem-radar",
        policy="PERIODIC", cadence_seconds=3600, authority="deploy-crontab",
        materialization_policy="WHEN_WORK_ATTEMPTED")
    trace = straces.make_trace(
        trace_id="TOEM", clank_id="oem-radar", scheduler_type="cron",
        observed_at=NOW, invoked_at="2026-08-25T05:55:00Z",
        process_started=True, execution_result="no_work_due",
        execution_detail="done: 0 source(s) crawled, 0 snapshot(s), 0 event(s)",
        evidence_source="journal")
    payload = _oem_real_shape_fixture(tmp_path)
    synth = syn.synthesize_fleet(payload, stale_hours=48.0,
                                 liveness_expectations=[exp],
                                 scheduler_traces=[trace])
    claim = synth["clanks"]["oem-radar"]
    # operational health independently HEALTHY-ish per its own plane...
    assert claim["state"] in ("HEALTHY", "UNKNOWN")
    # ...and execution plane says: fired, executed, intentional no-work.
    lv = claim["liveness"]
    assert lv["liveness_state"] == "NO_WORK_DUE"
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "NO"

    ledger = ano.detect([payload], liveness_expectations=[exp],
                        scheduler_traces=[trace])
    assert not any(a["type"] == "MATERIALIZATION_GAP" for a in ledger)


def test_oem_real_shape_detect_integration_positive_control(tmp_path):
    """Same shape but WITHOUT no-work evidence on an ALWAYS lane -> the
    anomaly MUST fire. Proves the fix didn't blanket-suppress gaps."""
    exp = live.make_expectation(
        expectation_id="EXP-OEM2", clank_id="oem-radar",
        policy="PERIODIC", cadence_seconds=3600, authority="deploy-crontab",
        materialization_policy="ALWAYS")
    trace = straces.make_trace(
        trace_id="TOEM2", clank_id="oem-radar", scheduler_type="cron",
        observed_at=NOW, invoked_at="2026-08-25T05:55:00Z",
        process_started=True, evidence_source="journal")
    payload = _oem_real_shape_fixture(tmp_path)
    ledger = ano.detect([payload], liveness_expectations=[exp],
                        scheduler_traces=[trace])
    assert any(a["type"] == "MATERIALIZATION_GAP" for a in ledger)


def test_capability_states_canonical_vocabulary_enforced(tmp_path):
    from clank_fleet.adapters.oem_radar import OemRadarAdapter
    store = tmp_path / "s"
    store.mkdir()
    db = store / "oem_radar.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE crawler_runs (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    cs = OemRadarAdapter(db_path=db).capability_states()
    from clank_runtime.contracts.capabilities import \
        validate_capability_states, CapabilityState
    assert validate_capability_states(cs) == []
    canonical = {s.value for s in CapabilityState}
    for domain, statement in cs.items():
        assert statement["state"] in canonical, domain

    # snapshot-level validation surfaces non-canonical states as warnings:
    class Rogue:
        def __init__(self, db_path):
            self.db_path = db_path
        def identity(self):
            class D: clank_version = "x"
            return D()
        def capabilities(self):
            class C: supports_delivery_accounting = False
            return C()
        def capability_states(self):
            return {"collection": {"state": "kinda_ok", "evidence": "trust"}}
    built = {"adapters": {"rogue": Rogue(db)}, "versions": {},
             "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, _ = snap.build_snapshot(inventory_path=inv,
                                     adapters_result=built,
                                     real_state_dir=tmp_path,
                                     out_dir=tmp_path)
    block = payload["clanks"]["rogue"]
    assert any("non-canonical state" in v for v in
               block.get("capability_states_violations", []))
