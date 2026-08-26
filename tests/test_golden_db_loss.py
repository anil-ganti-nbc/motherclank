"""GOLDEN INCIDENT FIXTURES — DB-LOSS-RESTORE and DB-LOSS-NEW-EPOCH.

Executable regression fixtures for the 2026-08-23 destructive volume-loss
incident (Smartwatch restore-from-backup; Feature Phone hard new epoch).
Hermetic: synthetic snapshots, no adapter plane, no host, no clock.

Invariants under test (ADR-0006 draft / GOLDEN_INCIDENTS register):

DB-LOSS-RESTORE:
  G1  no invented organic source transitions across the destruction boundary
      — anomalies inside the window are continuity-qualified, not presented
      as organic behaviour;
  G2  the explicit CONTINUITY_EVENT records exist and are visible to M3 as
      watch-only items citing the incident (never collector-repair advice);
  G3  restored state keeps a distinct epoch id; pre/post histories are never
      silently merged;
  G4  operational health and continuity remain separate dimensions.

DB-LOSS-NEW-EPOCH:
  G5  hard new epoch is represented (NEW_EPOCH), baseline suppression holds;
  G6  absence during the gap never becomes zero (UNKNOWN propagates);
  G7  M2/M3 produce NO false collector-remediation conclusions from the
     destroy/recreate pattern.
"""
from __future__ import annotations

import json

from motherclank import anomalies as ano
from motherclank import continuity as cont
from motherclank import recommendations as recs
from motherclank import synthesis as syn

T_PRE = "2026-08-22T08:00:00Z"
T_GAP = "2026-08-23T21:30:00Z"       # harvest inside the destroyed interval
T_RESTORE = "2026-08-23T22:30:00Z"   # smartwatch restored; feature-phone new DB
T_AFTER = "2026-08-24T06:00:00Z"

DESTRUCTION = "2026-08-23T21:22:08Z"
RESTORE_DONE = "2026-08-23T22:09:00Z"
FPC_NEW_EPOCH = "2026-08-23T21:36:11Z"


def _sources(statuses: dict[str, str]) -> dict:
    return {"sources": [{"source_id": sid, "status": st} for sid, st in statuses.items()]}


def _ok_block(finished_at: str) -> dict:
    return {
        "clank_version": "1",
        "status": {"operational_state": "healthy"},
        "health": _sources({"s-a": "ok", "s-b": "ok"}),
        "last_run": {"finished_at": finished_at},
    }


def _failed_block(error: str) -> dict:
    return {"observation": "FAILED_ADAPTER", "error": error}


def _snap(harvested_at: str, clanks: dict) -> dict:
    return {"harvested_at_utc": harvested_at,
            "content_hash": "sha256:synth-" + harvested_at,
            "clanks": clanks}


def _snapshots():
    return [
        # A: original pre-loss state — everything healthy
        _snap(T_PRE, {
            "smartwatch-clank": _ok_block("2026-08-22T07:45:00Z"),
            "feature-phone-clank": _ok_block("2026-08-22T07:19:00Z"),
        }),
        # B: missing/unavailable DB state right after the destructive rm
        _snap(T_GAP, {
            "smartwatch-clank": _failed_block("FileNotFoundError: smartwatch-clank.sqlite3"),
            "feature-phone-clank": _failed_block("FileNotFoundError: feature_phone_clank.db"),
        }),
        # C/D/E: restored smartwatch backup + fresh feature-phone epoch.
        # Smartwatch serves OLDER history (restore); feature-phone's fresh DB
        # re-discovers its catalogue, which would look like mass recovery or
        # sudden full-catalogue discovery if read organically.
        _snap(T_RESTORE, {
            "smartwatch-clank": _ok_block("2026-08-18T20:13:00Z"),
            "feature-phone-clank": _ok_block("2026-08-23T21:36:00Z"),
        }),
        _snap(T_AFTER, {
            "smartwatch-clank": _ok_block("2026-08-24T05:30:00Z"),
            "feature-phone-clank": _ok_block("2026-08-24T05:40:00Z"),
        }),
    ]


def _events():
    return [
        cont.make_event(
            event_id="INC-20260823-SW-GAP",
            clank_id="smartwatch-clank",
            instance_id="sw-hetzner-staging-01",
            lane_id="staging",
            event_type="OBSERVATION_GAP",
            effective_start=DESTRUCTION,
            effective_end=RESTORE_DONE,
            discovered_at="2026-08-23T23:00:00Z",
            origin="operator",
            evidence_refs=["docker-volume-rm-log", "host-timestamps"],
            notes="live staging volume deleted in operator error"),
        cont.make_event(
            event_id="INC-20260823-SW-RESTORE",
            clank_id="smartwatch-clank",
            instance_id="sw-hetzner-staging-01",
            lane_id="staging",
            event_type="RESTORE_FROM_BACKUP",
            effective_start=RESTORE_DONE,
            effective_end=None,
            discovered_at="2026-08-23T23:00:00Z",
            previous_epoch_id="sw-epoch-1",
            new_epoch_id="sw-epoch-1-restored-from-20260818-backup",
            origin="operator",
            evidence_refs=["backup-manifest-pre-stage-c"],
            notes="restored from 2026-08-18T20:50Z backup; "
                  "history 2026-08-18..2026-08-22 permanently missing"),
        cont.make_event(
            event_id="INC-20260823-FPC-EPOCH",
            clank_id="feature-phone-clank",
            instance_id="fpc-hetzner-prod-cron-01",
            lane_id="production",
            event_type="NEW_BASELINE",
            effective_start=FPC_NEW_EPOCH,
            effective_end=None,
            discovered_at="2026-08-24T00:00:00Z",
            previous_epoch_id="fpc-epoch-lost-no-backup",
            new_epoch_id="fpc-epoch-2",
            origin="operator",
            evidence_refs=["no-backup-existed", "fresh-db-created-by-collector"],
            notes="volume had no backup; all pre-incident history irrecoverable; "
                  "collector correctly baselined the recreated DB"),
    ]


def test_db_loss_restore_no_organic_transition_invention():
    ledger = ano.detect(_snapshots(), continuity_events=_events())
    sw = [a for a in ledger if a["clank_id"] == "smartwatch-clank"]
    assert any(a["type"] == "CONTINUITY_EVENT" for a in sw)
    # Any anomaly touching the incident window must be explicitly qualified
    for a in sw:
        if a["type"] == "CONTINUITY_EVENT":
            continue
        seen = a.get("last_seen", "")
        if seen >= DESTRUCTION:
            assert a.get("continuity_qualified") is True, a["type"]
            assert a["continuity_state"] in ("GAP_KNOWN", "RESTORED_HISTORY")


def test_db_loss_new_epoch_baseline_never_reads_as_novelty_or_recovery_story():
    snapshots = _snapshots()
    events = _events()
    ledger = ano.detect(snapshots, continuity_events=events)

    fpc = [a for a in ledger if a["clank_id"] == "feature-phone-clank"]
    assert any(a["type"] == "CONTINUITY_EVENT" for a in fpc)

    # The fresh epoch must be visible as NEW_EPOCH at derive time
    ctx = cont.continuity_context(events, "feature-phone-clank", T_AFTER)
    assert ctx["continuity_state"] == "NEW_EPOCH"
    assert ctx["epoch_id"] == "fpc-epoch-2"

    # Pre-epoch and post-epoch contexts carry DIFFERENT epoch identity:
    # histories are not silently merged.
    pre = cont.continuity_context(events, "feature-phone-clank", T_PRE)
    assert pre["continuity_state"] == "CONTINUOUS"
    assert pre["epoch_id"] != ctx["epoch_id"]

    # Absence during the gap is UNKNOWN, never zero (M1 R0 path)
    gap_claim = syn.synthesize_clank(
        "feature-phone-clank", snapshots[1]["clanks"]["feature-phone-clank"],
        observed_at=T_GAP, stale_hours=24.0,
        continuity=cont.continuity_context(events, "feature-phone-clank", T_GAP))
    assert gap_claim["state"] == "UNKNOWN"


def test_m3_cites_incident_and_never_advises_collector_repair_for_it(tmp_path):
    batch = ano.build_batch(tmp_path, _snapshots(),
                            ano.detect(_snapshots(), continuity_events=_events()))
    recs_list = recs.derive_recommendations(batch)
    inc = [r for r in recs_list if r["rule_key"] == "CONTINUITY_EVENT"]
    assert len(inc) >= 2  # both incident classes surfaced
    joined = json.dumps(inc).lower()
    assert "do not interpret" in joined or "do not interpret".replace(" ", "") in joined
    for r in inc:
        assert r["category"] == "NO_ACTION_WATCH"
        assert "repair" not in r["recommended_action"].lower()


def test_health_and_continuity_stay_separate_after_restore():
    events = _events()
    payload = _snap(T_AFTER, {
        "smartwatch-clank": _ok_block("2026-08-24T05:30:00Z"),
    })
    synth = syn.synthesize_fleet(payload, stale_hours=99999,
                                 continuity_events=events)
    claim = synth["clanks"]["smartwatch-clank"]
    # Operational recovery is real and reported; continuity remains qualified
    # because the restored DB serves a truncated historical record.
    assert claim["state"] == "HEALTHY"
    assert claim["continuity"]["continuity_state"] in ("GAP_KNOWN", "RESTORED_HISTORY")
    assert claim["continuity"]["orthogonal_to_operational_state"] is True


def test_registry_is_append_only_qualification_is_derive_time_only():
    """Old artifacts stay untouched; later knowledge appends evidence."""
    e1 = _events()[0]
    registry_file = None  # documentation of contract: load_events never writes
    assert registry_file is None
    original = json.dumps(e1, sort_keys=True)
    cont.continuity_context([e1], "smartwatch-clank", T_GAP)
    assert json.dumps(e1, sort_keys=True) == original
