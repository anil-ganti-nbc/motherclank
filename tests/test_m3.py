"""Motherclank M3 tests — advisory recommendations from the anomaly ledger.

Specimens mandated by the M3 brief: persistent HMD/Nokia failure,
SK hynix degradation, Casio Japan blocked state, Google Store degraded
state, and the recovered smartphone stale episode.
"""
from __future__ import annotations

import pytest

from motherclank.recommendations import (
    derive_recommendations, build_batch, read_latest_anomaly_batch)


def _anom(atype, clank, subject="s1", lifecycle="ONGOING", severity="HIGH",
          aid=None, evidence_detail=None):
    return {
        "anomaly_id": aid or f"aid-{atype}-{clank}-{subject}",
        "type": atype, "severity": severity, "clank_id": clank,
        "subject": subject,
        "first_seen": "2026-08-22T06:40:03+00:00",
        "last_seen": "2026-08-22T07:39:55+00:00",
        "lifecycle": lifecycle,
        "evidence": [{"observed_at": "2026-08-22T07:39:55+00:00",
                      "detail": evidence_detail or f"{atype} observed"}],
    }


def _batch(anomalies):
    return {"batch_generated_from": "2026-08-22T07:39:55+00:00",
            "batch_hash": "sha256:input", "anomalies": anomalies}


# ---------------------------------------------------------------------------
# Taxonomy: every anomaly class maps to the right category/priority/action
# ---------------------------------------------------------------------------

def test_persistent_streak_maps_to_upstream_remediation():
    recs = derive_recommendations(_batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia",
              severity="HIGH", evidence_detail="failed for 7 consecutive observations")]))
    assert len(recs) == 1
    r = recs[0]
    assert r["category"] == "UPSTREAM_CLANK_REMEDIATION"
    assert r["priority"] == "P1"
    assert r["status"] == "ACTIVE"
    assert "hmd-nokia" in r["recommended_action"]
    # citation carries the anomaly id + latest evidence verbatim
    assert r["cited_anomalies"][0]["latest_evidence"].startswith("failed for 7")


def test_first_observation_degraded_maps_to_watch():
    recs = derive_recommendations(_batch([
        _anom("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "smartphone-clank",
              "google_store_category_phones", severity="MEDIUM")]))
    r = recs[0]
    assert r["category"] == "NO_ACTION_WATCH" and r["priority"] == "P3"


def test_scheduler_classes_map_to_inspection():
    for atype in ("STALE_RUN_ACTIVE",):
        pass
    recs = derive_recommendations(_batch([
        _anom("SCHEDULER_INVOCATION_WITHOUT_WORK", "semiconductor-intelligence",
              "operational-scheduler")]))
    assert recs[0]["category"] == "DEPLOYMENT_SCHEDULER_INSPECTION"
    recs2 = derive_recommendations(_batch([
        _anom("REVISION_DRIFT", "watch-clank", "/home/anilganti/watch-clank",
              severity="MEDIUM")]))
    assert recs2[0]["category"] == "DEPLOYMENT_SCHEDULER_INSPECTION"


# ---------------------------------------------------------------------------
# The five named real specimens (shape-matching the live ledger)
# ---------------------------------------------------------------------------

def test_live_specimens_classify_correctly():
    batch = _batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia",
              evidence_detail="failed for 7 consecutive observations"),
        _anom("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "korean-tech-wire",
              "SK hynix Newsroom Korea", severity="MEDIUM"),
        _anom("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "watch-clank",
              "casio_japan", severity="HIGH",
              evidence_detail="observed blocked_zero with no prior ok"),
        _anom("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "smartphone-clank",
              "google_store_category_phones", severity="MEDIUM"),
        _anom("STALE_RUN", "smartphone-clank", "*", lifecycle="RECOVERED",
              evidence_detail="recency rule fired"),
    ])
    recs = derive_recommendations(batch)
    by = {(r["rule_key"], r["clank_id"], r["subject"]): r for r in recs}

    hmd = by[("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia")]
    assert hmd["category"] == "UPSTREAM_CLANK_REMEDIATION" and hmd["status"] == "ACTIVE"

    sk = by[("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "korean-tech-wire",
             "SK hynix Newsroom Korea")]
    assert sk["category"] == "NO_ACTION_WATCH"

    casio = by[("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "watch-clank", "casio_japan")]
    # HIGH severity first-observation still lands in watch lane; streak rule owns escalation
    assert casio["priority"] == "P3"

    google = by[("SOURCE_DEGRADED_AT_FIRST_OBSERVATION", "smartphone-clank",
                 "google_store_category_phones")]
    assert google["category"] == "NO_ACTION_WATCH"

    stale = by[("watch:STALE_RUN", "smartphone-clank", "*")]
    assert stale["status"] == "CLOSED"
    assert stale["cited_anomalies"] == []
    assert stale["resolved_citations"][0]["lifecycle"] == "RECOVERED"


# ---------------------------------------------------------------------------
# Lifecycle / dedup behaviour
# ---------------------------------------------------------------------------

def test_repeated_anomalies_update_one_recommendation():
    b1 = derive_recommendations(_batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia", aid="A1")]))
    b2_anoms = [
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia", aid="A1"),
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia", aid="A2"),
    ]
    b2 = derive_recommendations(_batch(b2_anoms))
    assert len(b1) == 1 and len(b2) == 1, "no duplicate spam"
    ids = {c["anomaly_id"] for c in b2[0]["cited_anomalies"]}
    assert ids == {"A1", "A2"}, "citations accumulate under one recommendation"


def test_recovered_citations_close_recommendation():
    recs = derive_recommendations(_batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia", aid="A1",
              lifecycle="RECOVERED")]))
    assert recs[0]["status"] == "CLOSED"
    assert recs[0]["resolved_citations"][0]["anomaly_id"] == "A1"


def test_recovered_stale_episode_suppresses_active_recommendation():
    """The smartphone false-STALE episode must appear CLOSED/watch, never an
    active inspection demand once recovered."""
    recs = derive_recommendations(_batch([
        _anom("STALE_RUN", "smartphone-clank", "*", lifecycle="RECOVERED")]))
    assert [r["status"] for r in recs] == ["CLOSED"]


# ---------------------------------------------------------------------------
# False-positive safeguards
# ---------------------------------------------------------------------------

def test_unknown_only_input_yields_nothing():
    recs = derive_recommendations(_batch([
        _anom("SOME_UNMAPPED_TYPE", "x", "y"),
        _anom("ANOTHER", "y", "z", lifecycle="RECOVERED"),
    ]))
    active = [r for r in recs if r["status"] == "ACTIVE"]
    assert active == []


FORBIDDEN_VERBS = ["delete ", "drop ", "rotate ", "restart ", "reboot ",
                   "redeploy ", "push ", "merge ", "kill ", "force "]


def test_no_action_template_proposes_destructive_or_mutating_action():
    from motherclank import recommendations as R
    for atype, rule in R._RULES.items():
        text = (rule["title"] + " " + rule["action"]).lower()
        for verb in FORBIDDEN_VERBS:
            assert verb not in text, f"{atype} action proposes '{verb}'"


def test_every_record_is_advisory_and_deterministic():
    recs = derive_recommendations(_batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia")]))
    p = recs[0]["provenance"]
    assert p["advisory_only"] is True and p["deterministic"] is True
    again = derive_recommendations(_batch([
        _anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia")]))
    assert recs[0]["chain_hash"] == again[0]["chain_hash"], \
        "same inputs must produce byte-identical recommendations"


def test_batch_chaining(tmp_path):
    batch_in = _batch([_anom("PERSISTENT_BLOCKED_STREAK", "fpc", "hmd-nokia")])
    p1 = build_batch(tmp_path, batch_in, derive_recommendations(batch_in))
    from motherclank.recommendations import append_batch
    append_batch(tmp_path, p1)
    batch_in2 = dict(batch_in, batch_generated_from="2026-08-23T07:00:00+00:00",
                     batch_hash="sha256:input2")
    p2 = build_batch(tmp_path, batch_in2, derive_recommendations(batch_in2))
    assert p2["previous_batch_hash"] == p1["batch_hash"]


def test_real_state_recommendations_from_live_ledger(tmp_path):
    import os as _os
    var_dir = _os.environ.get("M2_VAR_DIR")
    if not var_dir or not Path(var_dir).exists():
        pytest.skip("no live ledger available")
    batch = read_latest_anomaly_batch(Path(var_dir))
    recs = derive_recommendations(batch)
    keys = {(r["rule_key"], r["subject"]) for r in recs}
    assert ("PERSISTENT_BLOCKED_STREAK", "hmd-nokia") in keys
    sk = [r for r in recs if r["subject"] == "SK hynix Newsroom Korea"]
    assert sk and sk[0]["category"] in ("NO_ACTION_WATCH",
                                        "UPSTREAM_CLANK_REMEDIATION")
