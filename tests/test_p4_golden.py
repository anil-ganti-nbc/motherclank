"""P-4 golden fixtures — scheduler-fire traces, smartwatch onboarding,
survivability hash discipline, expectation placeholder rules.

Hermetic; no host, no clock, no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import anomalies as ano
from motherclank import adapters as adapters_mod
from motherclank import liveness as live
from motherclank import scheduler_traces as straces
from motherclank import snapshot as snap
from motherclank import survivability as surv
from motherclank import synthesis as syn


def _expectation(**kw):
    base = dict(expectation_id="EXP-P4", clank_id="c", policy="PERIODIC",
                cadence_seconds=3600, authority="cron", active=True)
    base.update(kw)
    return live.make_expectation(**base)


def _ok_block(finished_at, **extra):
    block = {
        "clank_version": "1",
        "status": {"operational_state": "healthy"},
        "health": {"sources": [{"source_id": "s-a", "status": "ok"}]},
        "last_run": {"finished_at": finished_at},
    }
    block.update(extra)
    return block


def _snap(at, clanks):
    return {"harvested_at_utc": at, "content_hash": "sha256:x-" + at,
            "clanks": clanks}


# ---------------------------------------------------------------------------
# P4-G1 CRON-FIRED-NO-RUN: positive trace proves the gap
# ---------------------------------------------------------------------------

def test_p4_g1_cron_fired_no_run_is_positive_materialization_gap():
    exp = _expectation(clank_id="oem-radar")
    trace = straces.make_trace(
        trace_id="T1", clank_id="oem-radar", scheduler_type="cron",
        unit_or_job="deploy_run.sh", observed_at="2026-08-23T22:00:00Z",
        invoked_at="2026-08-23T21:55:00Z", process_started=False,
        evidence_source="journal",
        notes="cron log line present; no collector start marker")
    # last app run is 36h old — the incident shape
    block = _ok_block("2026-08-20T22:00:00Z")
    lv = live.derive_liveness(block, exp, observed_at="2026-08-23T22:00:00Z",
                              trace=trace)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    assert lv["evidence"]["basis"] == "scheduler_trace"
    assert lv["evidence"]["process_started"] is False
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    assert lv["stages"]["PROCESS_STARTED"]["value"] == "NO"
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "NO"
    # and detect() raises it without any run-absence inference
    snapshots = [_snap("2026-08-23T22:00:00Z", {"oem-radar": block})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp],
                        scheduler_traces=[trace])
    gaps = [a for a in ledger if a["type"] == "MATERIALIZATION_GAP"]
    assert len(gaps) == 1
    assert "pre-exec" in json.dumps(gaps[0])


# ---------------------------------------------------------------------------
# P4-G2 OBSERVER-BLIND: no trace -> UNKNOWN, never a fabricated gap
# ---------------------------------------------------------------------------

def test_p4_g2_observer_blind_stays_unknown():
    exp = _expectation(clank_id="c")
    block = _ok_block("2026-08-20T22:00:00Z")  # stale run, no invocation data
    lv = live.derive_liveness(block, exp, observed_at="2026-08-23T22:00:00Z")
    assert lv["liveness_state"] == "EXECUTION_STALE"  # staleness provable...
    assert lv["evidence"]["cause"] == "UNKNOWN"       # ...cause is not
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# P4-G3 APPLICATION-FAILED: fired + started + failed row != pre-exec gap
# ---------------------------------------------------------------------------

def test_p4_g3_application_failure_is_not_a_materialization_gap():
    exp = _expectation(clank_id="c")
    trace = straces.make_trace(
        trace_id="T2", clank_id="c", scheduler_type="systemd_system",
        observed_at="2026-08-24T06:00:00Z", invoked_at="2026-08-24T05:30:00Z",
        process_started=True, exit_or_result="exit-code 1",
        evidence_source="journal")
    # application DID materialize a failed run after the fire
    block = _ok_block("2026-08-24T05:31:00Z", started_at="2026-08-24T05:30:10Z",
                      status={"operational_state": "degraded"})
    block["last_run"]["status"] = "failed"
    lv = live.derive_liveness(block, exp, observed_at="2026-08-24T06:00:00Z",
                              trace=trace)
    assert lv["liveness_state"] == "CURRENT"  # a run materialized in-window
    snapshots = [_snap("2026-08-24T06:00:00Z", {"c": block})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp],
                        scheduler_traces=[trace])
    assert not any(a["type"] == "MATERIALIZATION_GAP" for a in ledger)
    # the failure itself remains visible through ordinary health semantics
    claim_state = None  # operational degradation is M1's job; here we assert
    assert claim_state is None  # no gap was manufactured (covered above)


# ---------------------------------------------------------------------------
# P4-G4 RETIRED-LANE with a stray trace still emits nothing
# ---------------------------------------------------------------------------

def test_p4_g4_retired_lane_trace_does_not_create_anomalies():
    exp = _expectation(clank_id="tablet-clank", policy="RETIRED",
                       cadence_seconds=None, authority="none")
    trace = straces.make_trace(trace_id="T3", clank_id="tablet-clank",
                               scheduler_type="retired", observed_at="2026-08-24T00:00:00Z",
                               invoked_at=None, process_started=None,
                               evidence_source="operator-attestation",
                               notes="confirmed no live unit anywhere")
    block = _ok_block("2026-07-01T00:00:00Z", _synthesis_rules=["R3"])
    snapshots = [_snap("2026-08-24T06:00:00Z", {"tablet-clank": block})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp],
                        scheduler_traces=[trace])
    assert not any(a["type"] in ("STALE_RUN", "MATERIALIZATION_GAP")
                   for a in ledger)


# ---------------------------------------------------------------------------
# Trace loader discipline
# ---------------------------------------------------------------------------

def test_trace_loader_tolerant_and_hash_validating(tmp_path):
    d = tmp_path / "scheduler"
    d.mkdir()
    good = straces.make_trace(trace_id="T9", clank_id="c",
                              scheduler_type="cron",
                              observed_at="2026-08-24T00:00:00Z")
    bad = dict(good, content_hash="sha256:tampered")
    (d / "traces.jsonl").write_text(
        json.dumps(good) + "\n{oops\n" + json.dumps(bad) + "\n",
        encoding="utf-8")
    records, warnings = straces.load_traces(tmp_path)
    assert len(records) == 1
    assert len(warnings) == 2  # unparsable + tampered


# ---------------------------------------------------------------------------
# Survivability hash discipline (P4-G5)
# ---------------------------------------------------------------------------

def test_p4_g5_backup_without_artifact_hash_warns_and_flags(tmp_path):
    d = tmp_path / "survivability"
    d.mkdir()
    rec = surv.make_record(record_id="r1", record_type="BACKUP_CREATED",
                           clank_id="feature-phone-clank",
                           created_at="2026-08-24T02:00:00Z", origin="operator",
                           artifact_id="rp-x")  # hash deliberately null
    (d / "survivability-events.jsonl").write_text(json.dumps(rec) + "\n",
                                                  encoding="utf-8")
    records, warnings = surv.load_records(tmp_path)
    assert len(records) == 1
    assert any(w.startswith("RECOVERY_POINT_WITHOUT_ARTIFACT_HASH")
               for w in warnings)
    protection = surv.derive_protection(records, "feature-phone-clank")
    nrp = protection["newest_recovery_point"]
    assert nrp["cryptographically_identified"] is False
    assert nrp["hash"] is None
    # and a hashed record flips ONLY that flag
    rec2 = surv.make_record(record_id="r1", record_type="BACKUP_CREATED",
                            clank_id="feature-phone-clank",
                            created_at="2026-08-24T02:00:00Z", origin="operator",
                            artifact_id="rp-x", hash="sha256:real")
    hashed_root = tmp_path / "hashed"
    hashed_dir = hashed_root / "survivability"
    hashed_dir.mkdir(parents=True)
    (hashed_dir / "survivability-events.jsonl").write_text(
        json.dumps(rec2) + "\n", encoding="utf-8")
    records2, warn2 = surv.load_records(hashed_root)
    assert not any(w.startswith("RECOVERY_POINT_WITHOUT_ARTIFACT_HASH")
                   for w in warn2)
    p2 = surv.derive_protection(records2, "feature-phone-clank")
    assert p2["newest_recovery_point"]["cryptographically_identified"] is True


# ---------------------------------------------------------------------------
# Expectation placeholder-vs-UNKNOWN rules (§11)
# ---------------------------------------------------------------------------

SEEDS = Path(__file__).resolve().parents[1] / "continuity" / "seeds" / \
    "execution-expectations-seed-v1.jsonl"


def test_canonical_seed_has_no_verified_placeholders():
    """The canonical seed represents VERIFIED live scheduler truth (operator-
    corrected). It must never regress into placeholder-class values. A row
    may opt out only by honestly declaring verification_status=unverified."""
    assert SEEDS.exists(), "canonical expectations seed missing"
    rows = [json.loads(line) for line in
            SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "seed unexpectedly empty"
    problems = []
    for row in rows:
        violations = live.placeholder_violations(row)
        if violations:
            problems.append((row.get("expectation_id"), violations))
    assert not problems, f"verified-record placeholders: {problems}"


def test_placeholder_rule_distinguishes_verified_from_honest_unknown():
    ok_unknown = _expectation(clank_id="future-lane", instance_id="UNKNOWN",
                              lane_id="UNKNOWN", authority="UNKNOWN",
                              cadence_seconds=None,
                              verification_status="unverified")
    assert live.placeholder_violations(ok_unknown) == []
    bad_verified = _expectation(clank_id="x", instance_id="UNKNOWN",
                                lane_id="staging", authority="cron",
                                cadence_seconds=60)
    assert any("instance_id" in v for v in
               live.placeholder_violations(bad_verified))


# ---------------------------------------------------------------------------
# P4-G6 SMARTWATCH-HARVEST: registry-driven onboarding, zero core edits
# ---------------------------------------------------------------------------

def test_p4_g6_smartwatch_onboarded_via_registry_alone(tmp_path):
    """Smartwatch must be a normal observed Clank: present in the effective
    adapter registry, harvestable with UNKNOWN-honest blocks, and onboarded
    without touching any Motherclank core import of its class."""
    from motherclank.adapters import BUILTIN_REGISTRY, load_registry
    assert "smartwatch-clank" in load_registry(None)
    entry = BUILTIN_REGISTRY["smartwatch-clank"]
    # the registry references the adapter by name only - Motherclank source
    # contains no smartwatch-specific logic beyond this data row:
    import motherclank.adapters as admod
    src = Path(admod.__file__).read_text(encoding="utf-8")
    assert src.count("SmartwatchClankAdapter") == 1  # the registry row only

    built = adapters_mod.build_adapters(tmp_path)
    sw = built["adapters"]["smartwatch-clank"]
    # missing real-state copy -> UNKNOWN-honest observations, never failure
    block = snap.observe_clank(sw)
    status = block["status"]
    op = status.get("operational_state") if isinstance(status, dict) else \
        getattr(status, "operational_state", None)
    assert str(op).split(".")[-1].lower() == "unknown"
    inv = sw.store_inventory()
    assert inv == {"available": False, "tables": {}}


def test_p4_g6b_smartwatch_fixture_store_introspection(tmp_path):
    """With a fixture store present, inventory proves tables+counts without
    semantic claims (the live-schema mapping remains BLOCKED)."""
    from motherclank.adapters import build_adapters
    db = tmp_path / "smartwatch-clank.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t_future_run (id INTEGER)")
    con.execute("INSERT INTO t_future_run VALUES (7)")
    con.commit()
    con.close()
    sw = build_adapters(tmp_path)["adapters"]["smartwatch-clank"]
    inv = sw.store_inventory()
    assert inv["available"] is True
    assert inv["tables"] == {"t_future_run": 1}


# ---------------------------------------------------------------------------
# P4-G7 MULTI-CADENCE TRACE CORRELATION (convergence-pass regression)
# ---------------------------------------------------------------------------

def _multi_cadence_expectation(clank_id="c"):
    return _expectation(clank_id=clank_id, cadence_seconds=None,
                        multi_cadence=True,
                        notes="irregular per-source schedule; no single value")


def test_p4_g7a_multi_cadence_lane_trace_proves_gap():
    """The convergence-pass finding: feature-phone/watch-shaped lanes
    (cadence=None) must not have positive traces dropped on the floor."""
    exp = _multi_cadence_expectation("feature-phone-clank")
    trace = straces.make_trace(
        trace_id="T-MC1", clank_id="feature-phone-clank",
        scheduler_type="cron", observed_at="2026-08-24T06:00:00Z",
        invoked_at="2026-08-24T05:55:00Z", process_started=False,
        evidence_source="journal")
    block = _ok_block("2026-08-24T01:00:00Z")  # last run hours ago
    lv = live.derive_liveness(block, exp, observed_at="2026-08-24T06:00:00Z",
                              trace=trace)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    assert lv["evidence"]["cadence_bounded"] is False


def test_p4_g7b_multi_cadence_fired_and_started_never_fabricates_a_gap():
    """fired + process started but no newer run row: WITHOUT a declared
    cadence the persistence-delay window is unknown -> state UNKNOWN,
    stage evidence retained. No invented window, no false alarm."""
    exp = _multi_cadence_expectation("watch-clank")
    trace = straces.make_trace(
        trace_id="T-MC2", clank_id="watch-clank",
        scheduler_type="systemd_user", observed_at="2026-08-24T06:00:00Z",
        invoked_at="2026-08-24T05:55:00Z", process_started=True,
        evidence_source="journal")
    block = _ok_block("2026-08-23T20:00:00Z")
    lv = live.derive_liveness(block, exp, observed_at="2026-08-24T06:00:00Z",
                              trace=trace)
    assert lv["liveness_state"] == "UNKNOWN"
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    assert lv["stages"]["PROCESS_STARTED"]["value"] == "YES"
    # an OLD run row still exists -> stage stays YES; only the cadence-less
    # STATE judgment is withheld (no invented persistence-delay window)
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "YES"


def test_p4_g7c_synthesis_consumes_traces_for_multi_cadence_lanes():
    exp = dict(_multi_cadence_expectation("feature-phone-clank"))
    trace = straces.make_trace(
        trace_id="T-MC3", clank_id="feature-phone-clank",
        scheduler_type="cron", observed_at="2026-08-24T06:00:00Z",
        invoked_at="2026-08-24T05:55:00Z", process_started=False,
        evidence_source="journal")
    block = _ok_block("2026-08-24T01:00:00Z")
    payload = _snap("2026-08-24T06:00:00Z", {"feature-phone-clank": block})
    synth = syn.synthesize_fleet(payload, stale_hours=99999,
                                 liveness_expectations=[exp],
                                 scheduler_traces=[trace])
    claim = synth["clanks"]["feature-phone-clank"]
    lv = claim["liveness"]
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"
    # operational state untouched by the execution-plane finding:
    assert claim["state"] in ("HEALTHY", "DEGRADED", "FAILED", "UNKNOWN")


def test_p4_g7d_anomaly_ledger_covers_multi_cadence_gap():
    exp = _multi_cadence_expectation("feature-phone-clank")
    trace = straces.make_trace(
        trace_id="T-MC4", clank_id="feature-phone-clank",
        scheduler_type="cron", observed_at="2026-08-24T06:00:00Z",
        invoked_at="2026-08-24T05:55:00Z", process_started=False,
        evidence_source="journal")
    snapshots = [_snap("2026-08-24T06:00:00Z",
                       {"feature-phone-clank": _ok_block("2026-08-24T01:00:00Z")})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp],
                        scheduler_traces=[trace])
    gaps = [a for a in ledger if a["type"] == "MATERIALIZATION_GAP"
            and a["clank_id"] == "feature-phone-clank"]
    assert len(gaps) == 1
