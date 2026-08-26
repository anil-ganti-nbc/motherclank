"""F6 continuity semantics — unit tests (ADR-0006 draft contract).

Hermetic: pure derivation over synthetic records; no clocks, network, or
adapter plane required.
"""
from __future__ import annotations

import json

import pytest

from motherclank import continuity as cont
from motherclank import synthesis as syn


# ---------------------------------------------------------------------------
# Event construction and validation
# ---------------------------------------------------------------------------

def _event(**overrides):
    base = dict(
        event_id="EVT-TEST-001",
        clank_id="smartwatch-clank",
        event_type="OBSERVATION_GAP",
        effective_start="2026-08-23T21:22:08Z",
        effective_end="2026-08-23T22:09:00Z",
        discovered_at="2026-08-23T23:00:00Z",
        origin="operator",
    )
    base.update(overrides)
    return cont.make_event(**base)


def test_event_hash_is_content_addressed_and_tamper_evident():
    e = _event()
    assert e["content_hash"].startswith("sha256:")
    tampered = dict(e)
    tampered["effective_end"] = "2026-08-23T23:09:00Z"
    assert cont.validate_event(tampered)  # hash mismatch detected


@pytest.mark.parametrize("mutation", [
    {"event_type": "SOMETHING_ELSE"},
    {"origin": "nobody"},
    {"effective_start": "not-a-time"},
])
def test_invalid_events_are_rejected(mutation):
    fields = {**{"event_id": "X", "clank_id": "c", "event_type": "DATA_LOSS",
                 "effective_start": "2026-01-01T00:00:00Z",
                 "discovered_at": "2026-01-02T00:00:00Z", "origin": "operator"},
              **mutation}
    with pytest.raises(ValueError):
        cont.make_event(**fields)


def test_open_ended_event_is_valid_and_stays_open():
    e = _event(event_type="DATA_LOSS", effective_end=None,
               notes="pre-restore history permanently missing")
    assert e["effective_end"] is None
    assert cont.active_events([e], "smartwatch-clank", "2027-01-01T00:00:00Z")


def test_registry_loader_skips_malformed_lines_with_warnings(tmp_path):
    d = tmp_path / "continuity"
    d.mkdir()
    good = _event()
    bad_json = "{not json"
    invalid_event = json.dumps({**good, "content_hash": None, "origin": "alien"})
    (d / "continuity-events.jsonl").write_text(
        "\n".join([json.dumps(good), bad_json, invalid_event, ""]),
        encoding="utf-8")
    events, warnings = cont.load_events(tmp_path)
    assert len(events) == 1
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# Contexts, orthogonality, and the honesty properties
# ---------------------------------------------------------------------------

def test_context_states_across_the_incident_timeline():
    gap = _event()
    restore = _event(event_id="EVT-RESTORE", event_type="RESTORE_FROM_BACKUP",
                     effective_start="2026-08-23T22:09:00Z", effective_end=None,
                     previous_epoch_id="sw-epoch-1", new_epoch_id="sw-epoch-1-restored")
    loss = _event(event_id="EVT-LOSS", event_type="DATA_LOSS",
                  effective_start="2026-08-23T21:22:08Z", effective_end=None)
    events = [gap, restore, loss]

    before = cont.continuity_context(events, "smartwatch-clank", "2026-08-22T10:00:00Z")
    during = cont.continuity_context(events, "smartwatch-clank", "2026-08-23T21:30:00Z")
    after = cont.continuity_context(events, "smartwatch-clank", "2026-08-24T00:00:00Z")

    assert before["continuity_state"] == "CONTINUOUS"
    assert during["continuity_state"] == "GAP_KNOWN"
    # restoration never implies uninterrupted continuity
    assert after["continuity_state"] == "GAP_KNOWN" or \
           after["continuity_state"] == "RESTORED_HISTORY"
    assert after["continuity_state"] == "RESTORED_HISTORY"


def test_new_baseline_yields_new_epoch_never_novelty_interpretation():
    nb = _event(clank_id="feature-phone-clank", event_id="EVT-FPC-EPOCH",
                event_type="NEW_BASELINE",
                effective_start="2026-08-23T21:36:11Z", effective_end=None,
                previous_epoch_id="fpc-epoch-lost", new_epoch_id="fpc-epoch-2")
    ctx = cont.continuity_context([nb], "feature-phone-clank", "2026-08-24T00:00:00Z")
    assert ctx["continuity_state"] == "NEW_EPOCH"
    assert ctx["epoch_id"] == "fpc-epoch-2"


def test_operational_health_and_continuity_are_orthogonal():
    """collector HEALTHY after restore must still carry GAP_KNOWN continuity."""
    block = {
        "clank_version": "1",
        "status": {"operational_state": "healthy"},
        "health": {"sources": [{"source_id": "a", "status": "ok"}]},
        "last_run": {"finished_at": "2026-08-24T00:30:00Z"},
        "continuity": {"continuity_state": "GAP_KNOWN", "epoch_id": "sw-epoch-1-restored",
                       "active_event_ids": ["EVT-GAP"], "evidence_refs": []},
    }
    payload = {"harvested_at_utc": "2026-08-24T01:00:00Z",
               "clanks": {"smartwatch-clank": block}}
    result = syn.synthesize_fleet(payload, stale_hours=99999)
    claim = result["clanks"]["smartwatch-clank"]
    assert claim["state"] == "HEALTHY"
    assert claim["continuity"]["continuity_state"] == "GAP_KNOWN"
    assert claim["continuity"]["orthogonal_to_operational_state"] is True


def test_synthesis_without_continuity_events_leaves_claims_unqualified():
    block = {"status": {"operational_state": "healthy"},
             "health": {"sources": [{"source_id": "a", "status": "ok"}]},
             "last_run": {"finished_at": "2026-08-24T00:30:00Z"}}
    payload = {"harvested_at_utc": "2026-08-24T01:00:00Z",
               "clanks": {"c": block}}
    claim = syn.synthesize_fleet(payload)["clanks"]["c"]
    assert "continuity" not in claim


def test_snapshot_annotation_does_not_mutate_the_original_artifact():
    payload = {"harvested_at_utc": "2026-08-23T21:30:00Z",
               "clanks": {"c": {"status": {}}}}
    original = json.dumps(payload, sort_keys=True)
    e = _event(clank_id="c")
    annotated = cont.annotate_snapshot(payload, [e])
    assert json.dumps(payload, sort_keys=True) == original
    assert annotated["clanks"]["c"]["continuity"]["continuity_state"] == "GAP_KNOWN"
    assert annotated["continuity_registry_hash"]
