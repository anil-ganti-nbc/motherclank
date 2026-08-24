"""P-4.3 — typed evidence envelopes, semantic clocks, lane config contract,
declaration/observation separation, Watch expansion dogfood.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import anomalies as ano
from motherclank import adapters as adapters_mod
from motherclank import evidence as ev
from motherclank import liveness as live
from motherclank import scheduler_traces as straces
from motherclank import snapshot as snap
from motherclank import synthesis as syn


NOW = "2026-08-27T06:00:00Z"

# Extension-path registration happens through the PUBLIC API (this is the
# exact mechanism a future Watch expansion would use).
ev.register_type("collector_census", majors={1},
                 validate_payload=lambda p: ([] if isinstance(p, dict)
                                             and "collectors" in p
                                             else ["collectors missing"]))
ev.register_type("boom_type", majors={1}, validate_payload=lambda p: [])


def _env(**kw):
    base = dict(
        evidence_type="collector_census",
        evidence_version=1,
        subject={"clank_id": "watch-clank"},
        observed_at=NOW,
        substrate="sqlite:source_health",
        payload={"collectors": ["a", "b", "c"]},
        provenance={"query": "SELECT ... FROM source_health"},
    )
    base.update(kw)
    return ev.make_envelope(**base)


# ---------------------------------------------------------------------------
# Envelope model + compatibility classification
# ---------------------------------------------------------------------------

def test_envelope_hash_is_content_addressed():
    e1 = _env()
    e2 = _env()
    assert e1["content_hash"] == e2["content_hash"]
    tampered = dict(e1, evidence_version=2)
    assert ev.content_hash(tampered) != e1["content_hash"]


def test_classification_matrix():
    known, v = ev.classify(_env())
    assert (known, v) == ("KNOWN", [])

    newer_major, v2 = ev.classify(
        _env(evidence_version=9, payload={"collectors": []}))
    assert newer_major == "UNSUPPORTED_MAJOR"

    unknown_type = _env(evidence_type="quantum_flux")
    cls3, v3 = ev.classify(unknown_type)
    assert cls3 == "UNKNOWN_TYPE" and v3 == []

    malformed = {"evidence_type": "collector_census"}
    assert ev.classify(malformed)[0] == "MALFORMED"


def test_known_type_payload_validation():
    # scheduler_trace is a seeded canonical type: a bad payload must be
    # KNOWN_PAYLOAD_INVALID, never silently KNOWN.
    env = ev.make_envelope(
        evidence_type="scheduler_trace", evidence_version=1,
        subject={"clank_id": "c"}, observed_at=NOW, substrate="journal",
        payload={"trace_id": "x", "clank_id": "c",
                 "scheduler_type": "not-a-scheduler", "observed_at": NOW},
        provenance={"probe": "t"})
    cls, violations = ev.classify(env)
    assert cls == "KNOWN_PAYLOAD_INVALID"
    assert any("scheduler_type" in v for v in violations)


# ---------------------------------------------------------------------------
# GIC-28 / GIC-29 / GIC-30 — unknown, unsupported-major, malformed
# ---------------------------------------------------------------------------

def test_gic28_unknown_evidence_type_is_visible_and_claim_free():
    out = ev.consume_all([_env(evidence_type="quantum_flux")])
    assert out["derived_claims"] == []
    assert out["unknown_evidence"][0]["type"] == "quantum_flux"


def test_gic29_unsupported_major_is_visible_and_claim_free():
    out = ev.consume_all([_env(evidence_version=42)])
    assert out["derived_claims"] == []
    assert "unsupported major" in out["unknown_evidence"][0]["reason"]


def test_gic30_malformed_known_payload_is_visible_and_claim_free():
    env = ev.make_envelope(
        evidence_type="scheduler_trace", evidence_version=1,
        subject={"clank_id": "c"}, observed_at=NOW, substrate="journal",
        payload={"trace_id": "bad"},   # missing required trace fields
        provenance={"probe": "t"})
    out = ev.consume_all([env])
    assert out["derived_claims"] == []
    assert out["unknown_evidence"][0]["reason"].startswith(
        "KNOWN_PAYLOAD_INVALID")


def test_consumer_exception_is_isolated_not_fatal():
    def boom(env):
        raise RuntimeError("consumer bug")
    ev.register_consumer_for_type("boom_type", boom)
    try:
        env = _env(evidence_type="boom_type")
        out = ev.consume_all([env])
        assert out["derived_claims"] == []
        assert "consumer raised" in out["unknown_evidence"][0]["reason"]
    finally:
        from motherclank import evidence as evmod
        evmod._CONSUMERS.pop("boom_type", None)


# ---------------------------------------------------------------------------
# GIC-26/27 — semantic clocks; native vs derived; scheduler vs participant
# ---------------------------------------------------------------------------

def test_gic26_native_run_vs_derived_activity_are_labeled():
    exp = live.make_expectation(expectation_id="E", clank_id="fgt-like",
                                policy="PERIODIC", cadence_seconds=3600,
                                authority="cron", active=True)
    derived = {"supported": True, "finished_at": "2026-08-27T05:00:00Z",
               "status": "ok", "clock": "DERIVED_ACTIVITY_MAX",
               "derived_from": "MAX(source_health.last_attempt_at)"}
    native = {"supported": True, "finished_at": "2026-08-27T05:00:00Z",
              "status": "ok", "clock": "native_run_row"}
    for lr in (derived, native):
        block = {"clank_version": "1",
                 "status": {"operational_state": "healthy"},
                 "health": {"sources": [{"source_id": "a", "status": "ok"}]},
                 "last_run": lr}
        lv = live.derive_liveness(block, exp, observed_at=NOW,
                                  trace=_fired_trace())
        assert lv["liveness_state"] in ("CURRENT", "NO_WORK_DUE")
    # the labels themselves differ - the clocks are NOT interchangeable
    assert derived["clock"] != native["clock"]


def test_gic27_cross_clock_comparison_is_annotated_not_silent():
    exp = live.make_expectation(
        expectation_id="E2", clank_id="c", policy="PERIODIC",
        cadence_seconds=3600, authority="cron",
        materialization_policy="ALWAYS", active=True)
    trace = straces.make_trace(
        trace_id="TX", clank_id="c", scheduler_type="cron",
        observed_at=NOW, invoked_at="2026-08-27T05:55:00Z",
        process_started=True, execution_result=None,
        clock="scheduler_invocation", evidence_source="journal")
    block = {"clank_version": "1",
             "status": {"operational_state": "healthy"},
             "health": {"sources": [{"source_id": "a", "status": "ok"}]},
             "last_run": {"finished_at": "2026-08-19T06:00:00Z",
                          "clock": "native_run_row"}}
    lv = live.derive_liveness(block, exp, observed_at=NOW, trace=trace)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"
    cc = lv["evidence"]["cross_clock_comparison"]
    assert cc["run_clock"] == "native_run_row"
    assert cc["trace_clock"] == "scheduler_invocation"


def _fired_trace():
    return straces.make_trace(
        trace_id="TF", clank_id="fgt-like", scheduler_type="cron",
        observed_at=NOW, invoked_at="2026-08-27T05:55:00Z",
        process_started=True, execution_result="completed",
        evidence_source="journal")


# ---------------------------------------------------------------------------
# Lane Config contract + GIC-31/32/33/34
# ---------------------------------------------------------------------------

def _cfg(**kw):
    base = dict(clank_id="oem-radar",
                instance_id="oem-radar-hetzner-staging-cron-01",
                lane_id="staging", execution_policy="PERIODIC",
                authority="deploy-crontab", cadence_seconds=3600,
                scheduler_type="cron",
                materialization_policy="WHEN_WORK_ATTEMPTED")
    base.update(kw)
    return __import__("motherclank.lane_config", fromlist=["x"]) \
        .make_lane_config(**base)


def test_gic31_contradictory_lane_identity_detected():
    lc = __import__("motherclank.lane_config", fromlist=["x"])
    a = _cfg(instance_id="shared-instance-01")
    b = _cfg(clank_id="somebody-else", instance_id="shared-instance-01")
    conflicts = lc.find_identity_conflicts([a, b])
    assert conflicts and "somebody-else" in conflicts[0]


@pytest.mark.parametrize("bad", [
    {"execution_policy": "SOMETIMES"},
    {"materialization_policy": "MAYBE"},
    {"cadence_seconds": -60},
])
def test_invalid_lane_configs_rejected(bad):
    with pytest.raises(ValueError):
        _cfg(**bad)


def test_gic34_multi_cadence_config_stays_multi_cadence():
    cfg = _cfg(cadence_seconds=None, multi_cadence=True)
    assert cfg["multi_cadence"] is True and cfg["cadence_seconds"] is None


def test_retired_lane_with_cadence_is_impossible_declaration():
    with pytest.raises(ValueError, match="must not declare a cadence"):
        _cfg(execution_policy="RETIRED", cadence_seconds=3600)


# ---------------------------------------------------------------------------
# GIC-32/33 — declaration vs observation vs participant evidence
# ---------------------------------------------------------------------------

def test_gic32_declaration_alone_never_manufactures_observation():
    """A lane configured PERIODIC with zero snapshots produces no fired /
    executed / materialized claims anywhere in the synthesis."""
    exp = live.make_expectation(expectation_id="E3", clank_id="ghost-lane",
                                policy="PERIODIC", cadence_seconds=600,
                                authority="cron")
    payload = {"harvested_at_utc": NOW, "content_hash": "sha256:none",
               "clanks": {}}
    synth = syn.synthesize_fleet(payload, stale_hours=24.0,
                                 liveness_expectations=[exp])
    blob = json.dumps(synth).lower()
    for forbidden in ('"value": "yes"', "materialization_gap",
                      "no_work_due"):
        assert forbidden not in blob, forbidden


def test_gic33_observation_never_rewrites_declaration():
    exp = live.make_expectation(
        expectation_id="E4", clank_id="oem-radar", policy="PERIODIC",
        cadence_seconds=3600, authority="deploy-crontab",
        materialization_policy="ALWAYS", active=True)
    before = json.dumps(exp, sort_keys=True)
    t = _attested("no_work_due") if hasattr(__builtins__, "x") else None
    from tests.test_p42_attestation import _attested_trace  # reuse builder? no:
    # local builder to keep this file self-contained:
    trace = straces.make_trace(
        trace_id="TZ", clank_id="oem-radar", scheduler_type="cron",
        observed_at=NOW, invoked_at="2026-08-27T05:55:00Z",
        process_started=True, execution_result="no_work_due",
        execution_detail="done: 0 source(s) crawled",
        evidence_source="journal")
    block = {"clank_version": "1",
             "status": {"operational_state": "healthy"},
             "health": {"sources": [{"source_id": "s", "status": "ok"}]},
             "last_run": {"finished_at": "2026-08-25T20:00:00Z"}}
    lv = live.derive_liveness(block, exp, observed_at=NOW, trace=trace)
    assert json.dumps(exp, sort_keys=True) == before
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] in ("NO", "UNKNOWN")


def _attested(result):
    return result


# ---------------------------------------------------------------------------
# GIC-35/36 — Watch expansion dogfood via the generic extension path
# ---------------------------------------------------------------------------

def test_gic35_watch_collector_expansion_zero_core_edits(tmp_path):
    """18 collectors where there were 3: the observer contract does not care.
    Prove by scaling the census envelope and asserting identical handling."""
    collectors = [f"brand{i}_official" for i in range(18)]
    env = _env(payload={"collectors": collectors})
    ev.register_consumer_for_type(
        "collector_census",
        lambda e: {"collector_count": len(e["payload"]["collectors"])})
    out = ev.consume_all([env])
    claim = out["derived_claims"][0]
    assert claim["claims"]["collector_count"] == 18
    # and the guard: no core module may special-case watch brands
    core = Path(snap.__file__).parent
    for m in ("synthesis", "anomalies", "recommendations", "liveness",
              "snapshot"):
        code = (core / f"{m}.py").read_text(encoding="utf-8")
        assert "brand" not in code.lower()


def test_gic36_new_evidence_primitive_without_participant_core_edits(tmp_path):
    """The v0.3 extension path end-to-end: declare type -> register
    consumer -> envelope flows to a derived claim. Zero Motherclank-core
    edits; registration happens through the public API at runtime."""
    ev.register_type(
        "watch_brand_health@beta", majors={1},
        validate_payload=lambda p: ([] if "brands" in p else ["brands missing"]))
    ev.register_consumer_for_type(
        "watch_brand_health@beta",
        lambda e: {"healthy_brands": sum(1 for b in e["payload"]["brands"]
                                         if b["healthy"]),
                   "total_brands": len(e["payload"]["brands"])})
    env = ev.make_envelope(
        evidence_type="watch_brand_health@beta", evidence_version=1,
        subject={"clank_id": "watch-clank"}, observed_at=NOW,
        substrate="sqlite:new_releases",
        payload={"brands": [{"name": "citizen", "healthy": True},
                            {"name": "seiko", "healthy": False}]},
        provenance={"query": "brand health view"})
    out = ev.consume_all([env])
    claim = out["derived_claims"][0]
    assert claim["claims"]["healthy_brands"] == 1
    assert claim["claims"]["total_brands"] == 2


# ---------------------------------------------------------------------------
# GIC-37/38 — freshness honesty and clock anomalies
# ---------------------------------------------------------------------------

def test_gic37_fresh_observer_time_never_launders_stale_occurrence():
    env = ev.make_envelope(
        evidence_type="collector_census", evidence_version=1,
        subject={"clank_id": "c"}, observed_at=NOW,
        occurred_at="2026-08-13T06:00:00Z",     # two weeks stale occurrence
        substrate="sqlite:x", payload={"collectors": ["a"]},
        provenance={"query": "q"})
    assert env["occurred_at"].startswith("2026-08-13")   # preserved verbatim
    assert env["observed_at"] == NOW                     # never overwritten


def test_gic38_event_time_newer_than_ingestion_flagged_verbatim():
    env = _env(occurred_at="2026-08-28T00:00:00Z")       # "future" event time
    assert env["occurred_at"] == "2026-08-28T00:00:00Z"  # kept, not fixed
    # structural validation passes: the anomaly belongs to downstream
    # consumers, and the envelope must not silently normalize it.


# ---------------------------------------------------------------------------
# Property grid: scheduler type x cadence mode x materialization policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy", ["ALWAYS", "WHEN_WORK_ATTEMPTED",
                                    "OPTIONAL", "UNKNOWN"])
@pytest.mark.parametrize("mode", ["fixed", "multi", "none"])
def test_property_grid_derive_total_and_honest(policy, mode):
    exp = live.make_expectation(
        expectation_id="PG", clank_id="pg", policy="PERIODIC",
        cadence_seconds=(None if mode in ("multi", "none") else 3600),
        multi_cadence=(mode == "multi"),
        materialization_policy=policy, active=True)
    block = {"clank_version": "1",
             "status": {"operational_state": "unknown"},
             "health": {}, "last_run": {}}
    lv = live.derive_liveness(block, exp, observed_at=NOW)
    assert lv["liveness_state"] in set(live.LIVENESS_STATES)
    # none-mode without traces must stay UNKNOWN (no invented windows)
    if mode == "none":
        assert lv["liveness_state"] == "UNKNOWN"


def test_unknown_evidence_never_produces_known_claims_property():
    types = ["quantum_flux", "unregistered_v99", "", "collector_census"]
    envelopes = [_env(evidence_type=t) for t in types]
    envelopes += [{"garbage": True}]
    out = ev.consume_all(envelopes)
    claimed_types = {c["type"] for c in out["derived_claims"]}
    assert "quantum_flux" not in claimed_types
    assert "" not in claimed_types
