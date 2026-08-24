"""GOLDEN INCIDENT FIXTURES G1-G8 — continuity, liveness, survivability.

Hermetic; synthetic evidence only. Each fixture pins invariants from the
2026-08-22/23 incident families:

  Family A (execution-liveness): root `git stash -u` recreated logs/ as
  root:root; cron redirects failed BEFORE collector start; ~36h silent
  outage across oem-radar/smartwatch/feature-phone with zero application
  failure records.
  Family B (storage destruction): volume rm destroyed smartwatch (restored
  from backup) and feature-phone (no backup -> new epoch).
"""
from __future__ import annotations

import json

import pytest

from motherclank import anomalies as ano
from motherclank import continuity as cont
from motherclank import liveness as live
from motherclank import recommendations as recs
from motherclank import survivability as surv
from motherclank import synthesis as syn


def _ok_block(finished_at, started_at=None, status="healthy",
              sources=None, invocation=None):
    block = {
        "clank_version": "1",
        "status": {"operational_state": status},
        "health": {"sources": sources or [{"source_id": "s-a", "status": "ok"}]},
        "last_run": {"finished_at": finished_at},
    }
    if started_at:
        block["last_run"]["started_at"] = started_at
    if invocation:
        block["scheduler_pair"] = {"last_scheduler_invocation": invocation}
    return block


def _snap(at, clanks):
    return {"harvested_at_utc": at,
            "content_hash": "sha256:synth-" + at,
            "clanks": clanks}


def _expectation(**kw):
    base = dict(expectation_id="EXP-001", clank_id="c", policy="PERIODIC",
                cadence_seconds=3600, authority="cron", active=True)
    base.update(kw)
    return live.make_expectation(**base)


# ---------------------------------------------------------------------------
# G1 — SMARTWATCH RESTORE: gap + restored lineage, NOT a new epoch
# ---------------------------------------------------------------------------

def test_g1_smartwatch_restore_keeps_lineage_and_reports_gap():
    events = [
        cont.make_event(event_id="SW-GAP", clank_id="smartwatch-clank",
                        event_type="OBSERVATION_GAP",
                        effective_start="2026-08-22T10:00:00Z",
                        effective_end="2026-08-23T22:09:00Z",
                        discovered_at="2026-08-24T00:00:00Z", origin="operator"),
        cont.make_event(event_id="SW-RESTORE", clank_id="smartwatch-clank",
                        event_type="RESTORE_FROM_BACKUP",
                        effective_start="2026-08-23T22:09:00Z", effective_end=None,
                        discovered_at="2026-08-24T00:00:00Z", origin="operator",
                        previous_epoch_id="sw-epoch-1",
                        new_epoch_id="sw-epoch-1-restored-from-20260818"),
    ]
    ctx_after = cont.continuity_context(events, "smartwatch-clank",
                                        "2026-08-24T06:00:00Z")
    # restored lineage acknowledged; the bounded gap has closed but the
    # RESTORED_HISTORY state itself encodes that a discontinuity occurred
    assert ctx_after["continuity_state"] == "RESTORED_HISTORY"
    assert "SW-RESTORE" in ctx_after["active_event_ids"]
    # ...and explicitly NOT a new epoch
    assert ctx_after["epoch_id"] == "sw-epoch-1-restored-from-20260818"
    assert not any(e["event_type"] == "NEW_BASELINE" for e in events)

    payload = _snap("2026-08-24T06:00:00Z", {
        "smartwatch-clank": _ok_block("2026-08-24T05:30:00Z")})
    synth = syn.synthesize_fleet(payload, stale_hours=48.0,
                                 continuity_events=events)
    claim = synth["clanks"]["smartwatch-clank"]
    assert claim["state"] == "HEALTHY"          # current health can be healthy
    assert claim["continuity"]["continuity_state"] != "NEW_EPOCH"


# ---------------------------------------------------------------------------
# G2 — FEATURE PHONE TOTAL LOSS: hard epoch; old history unavailable
# ---------------------------------------------------------------------------

def test_g2_feature_phone_new_epoch_never_reads_as_organic_disappearance():
    events = [cont.make_event(
        event_id="FPC-EPOCH", clank_id="feature-phone-clank",
        event_type="NEW_BASELINE",
        effective_start="2026-08-23T21:36:11Z", effective_end=None,
        discovered_at="2026-08-24T00:00:00Z", origin="operator",
        previous_epoch_id="fpc-epoch-lost-no-backup", new_epoch_id="fpc-epoch-2")]
    pre = cont.continuity_context(events, "feature-phone-clank",
                                  "2026-08-22T09:00:00Z")
    post = cont.continuity_context(events, "feature-phone-clank",
                                   "2026-08-24T06:00:00Z")
    assert pre["epoch_id"] == "UNKNOWN" and post["epoch_id"] == "fpc-epoch-2"
    assert post["continuity_state"] == "NEW_EPOCH"
    snapshots = [
        _snap("2026-08-22T09:00:00Z", {"feature-phone-clank":
                                       _ok_block("2026-08-22T07:19:00Z")}),
        _snap("2026-08-23T21:30:00Z", {"feature-phone-clank":
                                       {"observation": "FAILED_ADAPTER",
                                        "error": "FileNotFoundError"}}),
        _snap("2026-08-24T06:00:00Z", {"feature-phone-clank":
                                       _ok_block("2026-08-24T05:40:00Z")}),
    ]
    ledger = ano.detect(snapshots, continuity_events=events)
    fpc = [a for a in ledger if a["clank_id"] == "feature-phone-clank"
           and a["type"] != "CONTINUITY_EVENT"]
    for a in fpc:
        if a.get("first_seen", "") >= "2026-08-23":
            assert a.get("continuity_qualified") is True


# ---------------------------------------------------------------------------
# G3 — PRE-EXEC FAILURE: materialization gap, never collector regression
# ---------------------------------------------------------------------------

def test_g3_preexec_failure_raises_materialization_gap_not_collector_fault():
    exp = _expectation(clank_id="oem-radar", materialization_policy="ALWAYS")
    # scheduler fired 5 min ago; newest run is 36 hours old (incident shape)
    block = _ok_block("2026-08-20T22:00:00Z",
                      invocation="2026-08-23T21:55:00Z")
    lv = live.derive_liveness(block, exp, observed_at="2026-08-23T22:00:00Z")
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"

    stages = lv["stages"]
    assert stages["SCHEDULE_EXPECTED"]["value"] == "YES"
    assert stages["SCHEDULER_FIRED"]["value"] == "YES"
    # strongest justified statement: scheduler evidence is NEWER than the
    # newest run row, so for THIS invocation a run positively did not
    # materialize (absence upgraded to NO only with positive contrary proof)
    assert stages["RUN_MATERIALIZED"]["value"] == "NO"
    assert stages["RUN_COMPLETED"]["value"] == "NO"
    assert stages["OUTCOME_RECORDED"]["value"] == "NO"

    snapshots = [_snap("2026-08-23T22:00:00Z", {"oem-radar": block})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp])
    gaps = [a for a in ledger if a["type"] == "MATERIALIZATION_GAP"]
    assert len(gaps) == 1
    recs_list = recs.derive_recommendations({
        "batch_generated_from": "2026-08-23T22:00:00Z", "batch_hash": "x",
        "anomalies": gaps})
    assert all(r["category"] == "DEPLOYMENT_SCHEDULER_INSPECTION"
               for r in recs_list if r["rule_key"] == "MATERIALIZATION_GAP")
    joined = json.dumps(recs_list).lower()
    assert "collector regression" in joined  # forbidden diagnosis is named and negated
    assert "do not diagnose collector regression" in joined


# ---------------------------------------------------------------------------
# G4 — INTENTIONAL DORMANCY: Tablet-class; stale artifact proves nothing
# ---------------------------------------------------------------------------

def test_g4_intentional_dormancy_emits_no_missing_run_anomaly():
    exp = _expectation(clank_id="tablet-clank", policy="RETIRED",
                       cadence_seconds=None, authority="none",
                       notes="finite soak completed; production manual/on-demand")
    block = _ok_block("2026-07-01T00:00:00Z")  # months-old run
    lv = live.derive_liveness(block, exp, observed_at="2026-08-24T06:00:00Z")
    assert lv["liveness_state"] == "INTENTIONALLY_DORMANT"
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "NOT_APPLICABLE"

    snapshots = [_snap("2026-08-24T06:00:00Z", {"tablet-clank": {
        **block, "_synthesis_rules": ["R3"]}})]
    ledger = ano.detect(snapshots, liveness_expectations=[exp])
    assert not any(a["type"] == "STALE_RUN" and a["clank_id"] == "tablet-clank"
                   for a in ledger)


# ---------------------------------------------------------------------------
# G5 — OBSERVER OUTAGE: unreadable evidence stays UNKNOWN, never NO
# ---------------------------------------------------------------------------

def test_g5_observer_outage_yields_unknown_not_missing_execution():
    exp = _expectation(clank_id="korean-tech-wire")
    block = {"observation": "FAILED_ADAPTER", "error": "permission denied"}
    lv = live.derive_liveness(block, exp, observed_at="2026-08-24T06:00:00Z")
    assert lv["liveness_state"] == "UNKNOWN"
    for stage in ("SCHEDULER_FIRED", "RUN_MATERIALIZED"):
        assert lv["stages"][stage]["value"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# G6/G7/G8 — backup evidence discipline
# ---------------------------------------------------------------------------

def _backup_records(artifact="rp-1", destination_class=None, drill=True):
    records = [surv.make_record(record_id="r1", record_type="BACKUP_CREATED",
                                clank_id="feature-phone-clank",
                                created_at="2026-08-24T02:00:00Z",
                                origin="operator", artifact_id=artifact,
                                hash="sha256:abc")]
    records.append(surv.make_record(
        record_id="r2", record_type="BACKUP_INTEGRITY_VERIFIED",
        clank_id="feature-phone-clank", created_at="2026-08-24T02:05:00Z",
        origin="tooling", artifact_id=artifact,
        verification_method="PRAGMA integrity_check", relates_to=artifact))
    if drill:
        records.append(surv.make_record(
            record_id="r3", record_type="RESTORE_DRILL_PASSED",
            clank_id="feature-phone-clank", created_at="2026-08-24T02:30:00Z",
            origin="operator", artifact_id=artifact, relates_to=artifact,
            verification_method="isolated disposable-volume restore"))
    if destination_class:
        records.append(surv.make_record(
            record_id="r4", record_type="BACKUP_TRANSFERRED_OFFHOST",
            clank_id="feature-phone-clank", created_at="2026-08-24T03:00:00Z",
            origin="operator", artifact_id=artifact, relates_to=artifact,
            destination_class=destination_class))
    return records


def test_g6_backup_existence_is_unverified():
    protection = surv.derive_protection(_backup_records(drill=False),
                                        "feature-phone-clank")
    assert protection["protection_state"] in ("UNVERIFIED", "INTEGRITY_VERIFIED")


def test_g7_restore_verified_chain_reaches_verified_and_durable_offhost():
    records = _backup_records(destination_class="durable")
    protection = surv.derive_protection(records, "feature-phone-clank")
    assert protection["protection_state"] == "RESTORE_VERIFIED"
    assert protection["off_host_durable"] is True
    assert protection["open_gaps"] == []


def test_g8_temporary_scratch_offhost_does_not_close_durability_gate():
    records = _backup_records(destination_class="temporary_scratch")
    protection = surv.derive_protection(records, "feature-phone-clank")
    assert protection["protection_state"] == "RESTORE_VERIFIED"  # RP itself verified
    assert protection["off_host_durable"] is False               # gate open
    assert any("durable off-host" in g for g in protection["open_gaps"])


def test_continuity_liveness_health_are_three_orthogonal_dimensions():
    """Smartwatch post-restore shape: HEALTHY + GAP_KNOWN + CURRENT.

    The open-ended DATA_LOSS event (permanently missing history) keeps
    continuity qualified even after the bounded observation gap closes."""
    events = [
        cont.make_event(event_id="SW-GAP", clank_id="smartwatch-clank",
                        event_type="OBSERVATION_GAP",
                        effective_start="2026-08-22T10:00:00Z",
                        effective_end="2026-08-23T22:09:00Z",
                        discovered_at="2026-08-24T00:00:00Z", origin="operator"),
        cont.make_event(event_id="SW-DATALOSS", clank_id="smartwatch-clank",
                        event_type="DATA_LOSS",
                        effective_start="2026-08-22T10:00:00Z",
                        effective_end=None,
                        discovered_at="2026-08-24T00:00:00Z", origin="operator"),
    ]
    expectations = [_expectation(clank_id="smartwatch-clank")]
    block = _ok_block("2026-08-24T05:30:00Z")
    claim = syn.synthesize_clank(
        "smartwatch-clank", block, observed_at="2026-08-24T06:00:00Z",
        stale_hours=48.0,
        continuity=cont.continuity_context(events, "smartwatch-clank",
                                           "2026-08-24T06:00:00Z"),
        liveness=live.derive_liveness(
            block, expectations[0], observed_at="2026-08-24T06:00:00Z"))
    assert claim["state"] == "HEALTHY"
    assert claim["continuity"]["continuity_state"] == "GAP_KNOWN"
    assert claim["liveness"]["liveness_state"] == "CURRENT"


@pytest.mark.parametrize("bad", [
    {"policy": "WHENEVER"},
    {"active": "yes"},
    {"cadence_seconds": -5},
])
def test_invalid_expectations_rejected(bad):
    fields = {**{"expectation_id": "X", "clank_id": "c", "policy": "PERIODIC"},
              **bad}
    with pytest.raises(ValueError):
        live.make_expectation(**fields)


def test_registry_loader_tolerates_malformed_lines(tmp_path):
    d = tmp_path / "liveness"
    d.mkdir()
    good = _expectation()
    (d / "execution-expectations.jsonl").write_text(
        json.dumps(good) + "\n{broken\n", encoding="utf-8")
    records, warnings = live.load_expectations(tmp_path)
    assert len(records) == 1 and len(warnings) == 1


def test_grace_multiplier_is_per_expectation_not_universal_law():
    """Reviewer constraint: x2 must not become universal. A lane may declare
    its own grace; two lanes with identical cadence but different grace
    reach MATERIALIZATION_GAP / EXECUTION_STALE at different instants."""
    exp_fast = _expectation(clank_id="a", cadence_seconds=3600,
                            grace_multiplier=1.1)
    exp_slow = _expectation(clank_id="b", cadence_seconds=3600)  # default 2.0
    # last run 75 minutes old: outside fast grace, within slow grace
    block = _ok_block("2026-08-23T20:45:00Z")
    at = "2026-08-23T22:00:00Z"
    assert live.derive_liveness(
        block, exp_fast, observed_at=at)["liveness_state"] == "EXECUTION_STALE"
    assert live.derive_liveness(
        block, exp_slow, observed_at=at)["liveness_state"] == "CURRENT"


def test_invalid_grace_multiplier_rejected():
    with pytest.raises(ValueError):
        _expectation(clank_id="c", grace_multiplier=-1)
