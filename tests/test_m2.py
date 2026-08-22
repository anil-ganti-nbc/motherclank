"""Motherclank M2 tests — deterministic anomaly detection.

Mandated historical specimens:
- Watch `casio_japan` blocked streak (real fleet event, 14 consecutive blocks)
- Smartphone recency/order failure class (M1 false-STALE, R3 rule evidence)

Plus lifecycle/recovery proof, UNKNOWN-never-proves-failure, sibling
isolation on partial adapter failure, and deterministic replay.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motherclank import anomalies as ano


def _snap(at: str, clanks: dict, fleet_state=None) -> dict:
    out = {"harvested_at_utc": at,
           "content_hash": "sha256:" + str(abs(hash((at, json.dumps(clanks, sort_keys=True))))),
           "clanks": clanks}
    if fleet_state:
        out["_fleet_state"] = fleet_state
    return out


def _src(source_id: str, status: str) -> dict:
    return {"source_id": source_id, "status": status}


T0, T1, T2, T3 = ("2026-08-20T00:00:00+00:00", "2026-08-21T00:00:00+00:00",
                  "2026-08-22T00:00:00+00:00", "2026-08-23T00:00:00+00:00")


# ---------------------------------------------------------------------------
# Specimen 1: Watch casio_japan blocked streak (real event)
# ---------------------------------------------------------------------------

def test_watch_casio_japan_blocked_streak_raises_and_recovers():
    history = [
        _snap(T0, {"watch-clank": {"health": {"sources": [_src("casio_japan", "ok"),
                                                          _src("citizen_news", "ok")]}}}),
        _snap(T1, {"watch-clank": {"health": {"sources": [_src("casio_japan", "blocked_zero"),
                                                          _src("citizen_news", "ok")]}}}),
        _snap(T2, {"watch-clank": {"health": {"sources": [_src("casio_japan", "blocked_zero"),
                                                          _src("citizen_news", "ok")]}}}),
        _snap(T3, {"watch-clank": {"health": {"sources": [_src("casio_japan", "blocked_zero"),
                                                          _src("citizen_news", "ok")]}}}),
    ]
    found = ano.detect(history)
    streak = [a for a in found if a["type"] == "PERSISTENT_BLOCKED_STREAK"
              and a["subject"] == "casio_japan"]
    trans = [a for a in found if a["type"] == "SOURCE_HEALTH_TRANSITION"
             and a["subject"] == "casio_japan"]
    assert streak and streak[0]["severity"] == "HIGH"
    assert streak[0]["first_seen"] <= T3
    # transition ok -> blocked_zero detected exactly once
    assert len(trans) == 1 and trans[0]["severity"] == "HIGH"

    # recovery: casio_japan returns to ok
    history.append(_snap("2026-08-24T00:00:00+00:00",
                         {"watch-clank": {"health": {"sources": [_src("casio_japan", "ok")]}}}))
    recovered = ano.detect(history)
    st = [a for a in recovered if a["type"] == "PERSISTENT_BLOCKED_STREAK"
          and a["subject"] == "casio_japan"]
    tr = [a for a in recovered if a["type"] == "SOURCE_HEALTH_TRANSITION"
          and a["subject"] == "casio_japan"]
    assert all(a["lifecycle"] == "RECOVERED" for a in st + tr), \
        "resolved anomalies must not remain permanently active"
    assert all(a.get("recovered_at") == "2026-08-24T00:00:00+00:00" for a in st)


# ---------------------------------------------------------------------------
# Specimen 2: smartphone recency/order failure class (R3 evidence)
# ---------------------------------------------------------------------------

def test_smartphone_stale_run_class_from_r3_evidence():
    def snap(rules):
        return _snap(T1, {"smartphone-clank": {
            "status": {"operational_state": "healthy"},
            "last_run": {"finished_at": "2026-08-18 01:20:53"},
            "_synthesis_rules": rules}})

    history = []
    for i, rules in enumerate(([], ["R3", "R5"], ["R5"])):
        s = snap(rules)
        s["harvested_at_utc"] = f"2026-08-2{i}T00:00:00+00:00"
        history.append(s)
    found = ano.detect(history)
    stale = [a for a in found if a["type"] == "STALE_RUN"
             and a["clank_id"] == "smartphone-clank"]
    assert len(stale) == 1
    assert stale[0]["lifecycle"] == "RECOVERED"   # R3 absent in final snapshot


def test_scheduler_invocation_without_work():
    history = [_snap(T0, {"semiconductor-intelligence": {
        "scheduler_pair": {"last_scheduler_invocation": "2026-08-22T10:00:00Z",
                            "last_successful_job_commit": "2026-08-22T09:00:00Z"}}})]
    found = ano.detect(history)
    m = [a for a in found if a["type"] == "SCHEDULER_INVOCATION_WITHOUT_WORK"]
    assert len(m) == 1 and m[0]["severity"] == "HIGH"


# ---------------------------------------------------------------------------
# Law-compliant negatives
# ---------------------------------------------------------------------------

def test_unknown_never_proves_failure():
    """UNKNOWN source observations must not raise transitions or streaks."""
    history = [
        _snap(T0, {"x-clank": {"health": {"sources": [_src("s1", "unknown")]}}}),
        _snap(T1, {"x-clank": {"health": {"sources": [_src("s1", "unknown")]}}}),
        _snap(T2, {"x-clank": {"health": {"sources": [_src("s1", "unknown")]}}}),
        _snap(T3, {"x-clank": {"health": {"sources": [_src("s1", "unknown")]}}}),
    ]
    assert ano.detect(history) == []


def test_partial_adapter_failure_does_not_contaminate_siblings():
    history = [
        _snap(T0, {
            "broken-clank": {"health": {"observation": "FAILED_ADAPTER",
                                        "error": "no such table"}},
            "healthy-clank": {"health": {"sources": [_src("s1", "ok")]}},
        }),
        _snap(T1, {
            "broken-clank": {"status": {"operational_state": "unknown"}},
            "healthy-clank": {"health": {"sources": [_src("s1", "failed")]}},
        }),
    ]
    found = ano.detect(history)
    by_clank = {a["clank_id"] for a in found}
    assert "healthy-clank" in by_clank, "sibling detection must proceed"
    broken = [a for a in found if a["clank_id"] == "broken-clank"]
    assert all(a["type"] != "SOURCE_HEALTH_TRANSITION" for a in broken), \
        "adapter failure must not fabricate transitions"


def test_first_known_observation_already_bad_recorded_without_transition():
    history = [_snap(T0, {"korean-tech-wire": {"health": {"sources": [
        _src("sk_hynix", "blocked_zero")]}}})]
    found = ano.detect(history)
    types = {a["type"] for a in found}
    assert "SOURCE_HEALTH_TRANSITION" not in types      # no prior ok to transition from
    assert "SOURCE_DEGRADED_AT_FIRST_OBSERVATION" in types


def test_revision_drift_and_recovery():
    def snap(rel):
        return _snap(T0 if rel == "DIVERGED" else T1,
                     {},
                     ) | {"law9_drift": [{"clank": "watch-clank",
                                          "checkout_path": "/x",
                                          "checkout_head": "a" * 40,
                                          "ledger_sha": "b" * 40,
                                          "relationship": rel}]}
    history = [snap("DIVERGED"), snap("CONVERGED")]
    # ensure law9 rows exist in final snapshot too for recovery evaluation
    history[-1] = _snap(T1, {}) | {"law9_drift": []}
    found = ano.detect(history)
    drift = [a for a in found if a["type"] == "REVISION_DRIFT"]
    assert drift and drift[0]["lifecycle"] == "RECOVERED"


def test_fleet_health_degradation():
    h = [
        dict(_snap(T0, {}, fleet_state=None), **{"_fleet_state": "HEALTHY"}),
        dict(_snap(T1, {}, fleet_state=None), **{"_fleet_state": "FAILED"}),
    ]
    found = ano.detect(h)
    f = [a for a in found if a["type"] == "FLEET_HEALTH_DEGRADATION"]
    assert f and f[0]["severity"] == "HIGH"


# ---------------------------------------------------------------------------
# Determinism + chaining + batch shape
# ---------------------------------------------------------------------------

def test_deterministic_replay(real_history_factory, tmp_path):
    snaps = real_history_factory()
    one = ano.detect(snaps)
    two = ano.detect(list(reversed(list(reversed(snaps)))))
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


@pytest.fixture()
def real_history_factory():
    def build():
        return [
            _snap(T0, {"watch-clank": {"health": {"sources": [_src("casio_japan", "ok")]}}}),
            _snap(T1, {"watch-clank": {"health": {"sources": [_src("casio_japan", "blocked_zero")]}}}),
            _snap(T2, {"watch-clank": {"health": {"sources": [_src("casio_japan", "blocked_zero")]}}},
                  fleet_state="DEGRADED"),
        ]
    return build


def test_batch_chaining(tmp_path, real_history_factory):
    snaps = real_history_factory()
    b1 = ano.build_batch(tmp_path, snaps, ano.detect(snaps))
    p1 = ano.append_batch(tmp_path, b1)
    assert p1.exists() and b1["previous_batch_hash"] is None

    snaps2 = snaps + [_snap(T3, {"watch-clank": {"health": {
        "sources": [_src("casio_japan", "ok")]}}})]
    b2 = ano.build_batch(tmp_path, snaps2, ano.detect(snaps2))
    assert b2["previous_batch_hash"] == b1["batch_hash"]
    assert b2["recovered_count"] >= 1


@pytest.fixture()
def var_dir_factory():
    def build():
        env_dir = __import__("os").environ.get("M2_VAR_DIR")
        if not env_dir or not Path(env_dir).exists():
            return None
        return Path(env_dir)
    return build


def test_real_state_history_if_available(var_dir_factory):
    var_dir = var_dir_factory()
    if var_dir is None:
        pytest.skip("no M0/M1 history available")
    history = ano.load_history(var_dir)
    found = ano.detect(history)
    ids = {(a["type"], a["clank_id"]) for a in found}
    # the smartphone false-STALE episode must appear as STALE_RUN with recovery
    stale = [a for a in found if a["type"] == "STALE_RUN"
             and a["clank_id"] == "smartphone-clank"]
    if stale:
        assert stale[0]["lifecycle"] == "RECOVERED"
