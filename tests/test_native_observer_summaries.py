"""Native summaries survive governed observation without child interpretation."""
from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from motherclank import contract, snapshot, synthesis
from test_snapshot_manifest_v1 import _build, _case, _iso


NOW = "2026-10-02T08:00:00Z"
SOURCE = [{"source_key": "source-a", "vendor": "vendor-a", "plane": "PRODUCT",
           "authority": "EXPERIMENTAL", "enabled": 0,
           "promotion_state": "EXPERIMENTAL", "registered_state": "REGISTERED"}]
DIAGNOSTIC = {
    "conditions": [{"source_key": "source-a", "diagnostic_type": "AMBIGUOUS",
                    "status": "OPEN", "reason": "identity uncertain", "n": 1}],
    "sightings_total": 3,
}
EVIDENCE = {
    "payload_version": "1.0", "availability": "AVAILABLE",
    "source_revision": "UNKNOWN", "deployed_revision": "UNKNOWN",
    "event_code_revision": "c" * 40,
    "scheduler_authority": "NONE", "delivery_authority": "NONE",
}
SUMMARY_NAMES = ("source_summary", "diagnostic_summary", "observer_evidence")


class LegacyAdapter:
    def identity(self):
        return SimpleNamespace(clank_version="1", contract_version="0.1.0-v3")

    def capabilities(self):
        return SimpleNamespace(supports_delivery_accounting=False,
                               supports_health=True, supports_telemetry=False)

    def status(self):
        return {"operational_state": "healthy"}

    def health(self):
        return {"sources": [{"source_id": "fixture", "status": "ok"}]}

    def last_run(self):
        return {"supported": True, "finished_at": NOW}

    def capability_states(self):
        return {"collection": {"state": "unknown_or_unverified", "evidence": "fixture"}}


class NativeSummaryAdapter(LegacyAdapter):
    def observer_evidence(self):
        return copy.deepcopy(EVIDENCE)

    def source_summary(self):
        return copy.deepcopy(SOURCE)

    def diagnostic_summary(self):
        return copy.deepcopy(DIAGNOSTIC)


def _claim(block):
    return synthesis.synthesize_clank("native-lane", block, observed_at=NOW, stale_hours=24)


def test_native_shapes_and_revision_distinctions_are_preserved_verbatim():
    block = snapshot.observe_clank(NativeSummaryAdapter())
    claim = _claim(block)
    for name, expected in zip(SUMMARY_NAMES, (SOURCE, DIAGNOSTIC, EVIDENCE)):
        assert block[name] == expected
        assert claim[name] == expected
        assert claim[name] is not block[name]
    assert claim["provenance"]["observer_extension_fields"] == [
        f"clanks.native-lane.{name}" for name in SUMMARY_NAMES]
    # Presence of uncertainty/policy evidence is not itself a generic source
    # failure rule; domain interpretation stays with the adapter.
    assert claim["state"] == _claim(snapshot.observe_clank(LegacyAdapter()))["state"]


@pytest.mark.parametrize("observation", ["CHILD_EXECUTION_STALE", "CHILD_EXECUTION_UNKNOWN"])
def test_unknown_early_return_retains_native_summaries(observation):
    block = snapshot.observe_clank(NativeSummaryAdapter())
    block["observation"] = observation
    claim = _claim(block)
    assert claim["state"] == "UNKNOWN"
    assert claim["rules_applied"] == ["R0_SNAPSHOT"]
    for name in SUMMARY_NAMES:
        assert claim[name] == block[name]


def test_missing_native_evidence_does_not_turn_empty_summary_into_zero_health():
    adapter = NativeSummaryAdapter()
    adapter.status = lambda: {"operational_state": "unknown"}
    adapter.health = lambda: {"sources": []}
    adapter.source_summary = lambda: []
    adapter.diagnostic_summary = lambda: {"conditions": [], "sightings_total": "UNKNOWN"}
    adapter.observer_evidence = lambda: {
        "payload_version": "1.0", "availability": "UNKNOWN", "reason": "DB_UNAVAILABLE"}
    claim = _claim(snapshot.observe_clank(adapter))
    assert claim["state"] == "UNKNOWN"
    assert claim["source_summary"] == []
    assert claim["diagnostic_summary"]["sightings_total"] == "UNKNOWN"
    assert claim["observer_evidence"]["availability"] == "UNKNOWN"


@pytest.mark.parametrize("value", [None, [], {}, {"payload_version": "2.0"},
                                   {"payload_version": "1.1"}])
def test_invalid_evidence_envelope_isolates_lane_and_does_not_probe_summaries(value):
    adapter = NativeSummaryAdapter()
    adapter.observer_evidence = lambda: value
    adapter.source_summary = lambda: pytest.fail("unqualified native summary invoked")
    adapter.diagnostic_summary = lambda: pytest.fail("unqualified native summary invoked")
    block = snapshot.observe_clank(adapter)
    assert block["observer_evidence"]["observation"] == "FAILED_ADAPTER"
    assert "source_summary" not in block and "diagnostic_summary" not in block
    assert _claim(block)["state"] == "UNKNOWN"
    assert _claim(snapshot.observe_clank(LegacyAdapter()))["state"] == "HEALTHY"


def test_raising_evidence_envelope_isolated_before_summary_dispatch():
    adapter = NativeSummaryAdapter()

    def raises():
        raise RuntimeError("evidence unavailable")

    adapter.observer_evidence = raises
    adapter.source_summary = lambda: pytest.fail("summary must require qualified evidence")
    block = snapshot.observe_clank(adapter)
    assert block["observer_evidence"]["observation"] == "FAILED_ADAPTER"
    assert "source_summary" not in block and "diagnostic_summary" not in block
    assert _claim(block)["state"] == "UNKNOWN"


@pytest.mark.parametrize("name,value", [("source_summary", {}), ("source_summary", [7]),
                                        ("diagnostic_summary", [])])
def test_invalid_summary_shape_is_visible_without_poisoning_sibling_extensions(name, value):
    adapter = NativeSummaryAdapter()
    setattr(adapter, name, lambda: value)
    block = snapshot.observe_clank(adapter)
    assert block[name]["observation"] == "FAILED_ADAPTER"
    assert block["observer_evidence"] == EVIDENCE
    assert _claim(block)["state"] == "UNKNOWN"
    assert _claim(block)[name] == block[name]


def test_raising_summary_isolated_and_diagnostic_evidence_survives():
    adapter = NativeSummaryAdapter()

    def raises():
        raise RuntimeError("native summary unavailable")

    adapter.source_summary = raises
    block = snapshot.observe_clank(adapter)
    assert block["source_summary"]["observation"] == "FAILED_ADAPTER"
    assert block["diagnostic_summary"] == DIAGNOSTIC
    assert _claim(block)["diagnostic_summary"] == DIAGNOSTIC
    assert _claim(block)["state"] == "UNKNOWN"


def test_legacy_five_lane_outputs_unchanged_without_opt_in():
    expected = snapshot.observe_clank(LegacyAdapter())
    for _ in range(5):
        adapter = LegacyAdapter()
        # Two production-qualified legacy adapters already have this method.
        # Merely sharing its name must not silently activate new transport.
        adapter.source_summary = lambda: pytest.fail("legacy summary unexpectedly invoked")
        adapter.diagnostic_summary = lambda: pytest.fail("legacy summary unexpectedly invoked")
        observed = snapshot.observe_clank(adapter)
        assert observed == expected
        assert _claim(observed) == _claim(expected)
        assert not set(SUMMARY_NAMES).intersection(observed)
        assert not set(SUMMARY_NAMES).intersection(contract.surface_report(adapter)["optional_extensions"])


def test_live_manifest_path_retains_stale_native_detail_and_cannot_mutate_source(tmp_path):
    row, manifest, inventory, built, adapter, save = _case(tmp_path)
    before = {key: hashlib.sha256(Path(row[key]).read_bytes()).hexdigest()
              for key in ("source_path", "snapshot_path")}
    adapter.observer_evidence = lambda: copy.deepcopy(EVIDENCE)
    adapter.source_summary = lambda: copy.deepcopy(SOURCE)
    adapter.diagnostic_summary = lambda: copy.deepcopy(DIAGNOSTIC)
    row["child_as_of"] = _iso(datetime.now(UTC) - timedelta(days=8))
    row["child_execution_freshness"] = "STALE"
    save()
    payload, warnings = _build(tmp_path, manifest, inventory, built)
    assert warnings == []
    block = payload["clanks"]["example-clank"]
    assert block["observation"] == "CHILD_EXECUTION_STALE"
    assert block["snapshot_provenance"]["intake_freshness_state"] == "FRESH"
    claim = synthesis.synthesize_fleet(payload)["clanks"]["example-clank"]
    assert claim["state"] == "UNKNOWN"
    for name in SUMMARY_NAMES:
        assert claim[name] == block[name]
    assert claim["provenance"]["snapshot_sha256"] == row["snapshot_sha256"]
    assert {key: hashlib.sha256(Path(row[key]).read_bytes()).hexdigest()
            for key in before} == before
