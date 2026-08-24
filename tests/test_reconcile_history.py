"""H-4 reconciler tests: read-only, window filtering, qualification mapping."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

from motherclank import continuity as cont

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_history.py"
_spec = importlib.util.spec_from_file_location("reconcile_history", SCRIPT)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)


def _run(var: Path, start: str, end: str) -> dict:
    return rh.reconcile(var, rh._parse(start), rh._parse(end))


def _seed_var(tmp_path: Path) -> Path:
    var = tmp_path / "var"
    (var / "snapshots").mkdir(parents=True)
    (var / "syntheses").mkdir(parents=True)
    (var / "continuity").mkdir(parents=True)

    snap = {"harvested_at_utc": "2026-08-23T21:30:00Z",
            "content_hash": "sha256:snap1",
            "clanks": {"feature-phone-clank": {
                "status": {"operational_state": "unknown"},
                "observation": "FAILED_ADAPTER", "error": "missing"}}}
    (var / "snapshots" / "2026-08-23.jsonl").write_text(
        json.dumps(snap) + "\n", encoding="utf-8")

    synth = {"synthesized_at_utc": "2026-08-23T21:31:00Z",
             "snapshot_hash": "sha256:snap1",
             "fleet_state": "UNKNOWN",
             "clanks": {"feature-phone-clank": {
                 "state": "UNKNOWN", "rules_applied": ["R0"]}}}
    (var / "syntheses" / "2026-08-23.jsonl").write_text(
        json.dumps(synth) + "\n", encoding="utf-8")

    event = cont.make_event(
        event_id="INC-X", clank_id="feature-phone-clank",
        event_type="NEW_BASELINE", effective_start="2026-08-23T21:36:11Z",
        effective_end=None, discovered_at="2026-08-24T00:00:00Z",
        origin="operator", previous_epoch_id="old", new_epoch_id="fpc-epoch-2")
    (var / "continuity" / "continuity-events.jsonl").write_text(
        json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")

    # an out-of-window artifact that must NOT appear
    outside = dict(snap, harvested_at_utc="2026-09-01T00:00:00Z",
                   content_hash="sha256:snap2")
    (var / "snapshots" / "2026-09-01.jsonl").write_text(
        json.dumps(outside) + "\n", encoding="utf-8")
    return var


def test_reconcile_maps_window_findings_with_qualification(tmp_path):
    var = _seed_var(tmp_path)
    report = _run(var, "2026-08-22T09:00:00Z", "2026-08-24T00:00:00Z")
    kinds = {f["artifact"] for f in report["findings"]}
    assert "snapshot" in kinds and "synthesis" in kinds
    # nothing from outside the window leaked in
    assert all(f["timestamp"] is None or f["timestamp"] <= "2026-08-24"
               for f in report["findings"])
    snap_f = next(f for f in report["findings"]
                  if f["artifact"] == "snapshot"
                  and f["clank_id"] == "feature-phone-clank")
    qual = snap_f["qualification"]
    assert qual["execution_policy"] == "UNKNOWN"  # no expectations registered


def test_reconcile_flags_interpretation_change_for_incident_clank(tmp_path):
    """A HEALTHY-era synthesis inside the window must be flagged as changing
    meaning once incident evidence is known."""
    var = _seed_var(tmp_path)
    healthy_synth = {"synthesized_at_utc": "2026-08-23T22:30:00Z",
                     "snapshot_hash": "sha256:none",
                     "fleet_state": "HEALTHY",
                     "clanks": {"feature-phone-clank": {
                         "state": "HEALTHY", "rules_applied": ["R5"]}}}
    (var / "syntheses" / "late.jsonl").write_text(
        json.dumps(healthy_synth) + "\n", encoding="utf-8")
    report = _run(var, "2026-08-22T09:00:00Z", "2026-08-24T00:00:00Z")
    flagged = [f for f in report["findings"]
               if f.get("interpretation_changes_with_incident_evidence")]
    assert flagged and all(f["clank_id"] == "feature-phone-clank"
                           for f in flagged)


def _tree_state(var: Path) -> list:
    return sorted(
        (str(p.relative_to(var)),
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in var.rglob("*") if p.is_file())


def test_reconcile_is_read_only(tmp_path):
    var = _seed_var(tmp_path)
    before = _tree_state(var)
    _run(var, "2026-08-01T00:00:00Z", "2026-09-30T00:00:00Z")
    after = _tree_state(var)
    assert before == after
