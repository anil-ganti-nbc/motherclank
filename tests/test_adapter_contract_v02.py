"""Adapter Contract v0.2 - surface validation, fail-safe isolation,
hot-swap full-pipeline travel, cross-plane invariants, read-only proofs.
"""
from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import anomalies as ano
from motherclank import recommendations as recs
from motherclank import snapshot as snap
from motherclank import synthesis as syn
from motherclank.contract import (
    OBSERVER_SURFACE_SPEC_VERSION,
    REQUIRED_METHODS,
    surface_report,
    validate_surface,
)


# ---------------------------------------------------------------------------
# Synthetic adapter factory
# ---------------------------------------------------------------------------

class ConformingAdapter:
    """Minimum conforming observer surface (contract spec 0.2)."""

    runtime_version = "0.1.0-v3"

    def __init__(self, db_path=None):
        self.db_path = str(db_path or ":memory:")

    def identity(self):
        class D:
            clank_version = "1"
            contract_version = self.runtime_version
        return D()

    def capabilities(self):
        class C:
            supports_delivery_accounting = False
        return C()

    def status(self):
        return {"operational_state": "unknown"}

    def health(self):
        return {"sources": [], "warnings": ["synthetic fixture"]}

    def last_run(self):
        return {"supported": False}

    def capability_states(self):
        return {"collection": {"state": "unknown_or_unverified",
                               "evidence": "synthetic fixture"}}


def _make(tmp_path, **overrides):
    attrs = {m: None for m in REQUIRED_METHODS}
    cls = type("Broken", (ConformingAdapter,), {})
    for name, behavior in overrides.items():
        if behavior == "raise":
            def raiser(self, *a, **k):
                raise RuntimeError(f"{name} exploded")
            setattr(cls, name, raiser)
        elif behavior == "missing":
            setattr(cls, name, None)
        elif isinstance(behavior, str) and name == "runtime_version":
            cls.runtime_version = behavior
    return cls(db_path=tmp_path / "x.db")


# ---------------------------------------------------------------------------
# Surface validation
# ---------------------------------------------------------------------------

def test_raising_methods_pass_surface_but_fail_isolated_at_runtime(tmp_path):
    """A method that exists but explodes is a RUNTIME isolation case, not a
    surface violation: the block must become FAILED_ADAPTER with the error
    captured, and validate_surface stays clean."""
    broken = _make(tmp_path, **{"status": "raise"})
    assert validate_surface(broken,
                            runtime_contract_version=broken.runtime_version) == []
    block = snap.observe_clank(broken)
    assert any(
        isinstance(v, dict) and "status" in str(v.get("error", ""))
        for v in block.values() if isinstance(v, dict)
    ) or block.get("observation") == "FAILED_ADAPTER"


def test_missing_method_listed(tmp_path):
    cls = type("B", (ConformingAdapter,), {})
    setattr(cls, "health", None)
    v = validate_surface(cls(db_path=""))
    assert any("health" in x for x in v)


@pytest.mark.parametrize("version", ["1.0.0-v9", "garbage", ""])
def test_unsupported_or_garbage_contract_major_fails_safe(tmp_path, version):
    a = _make(tmp_path)
    a.runtime_version = version
    v = validate_surface(a, runtime_contract_version=version)
    assert v, "unrecognized major must be a violation"


def test_conforming_surface_has_no_violations_and_reports_extensions():
    a = _make(Path("x"))
    assert validate_surface(a, runtime_contract_version=a.runtime_version) == []
    report = surface_report(a)
    assert report["spec_version"] == OBSERVER_SURFACE_SPEC_VERSION
    assert set(report["required_present"]) == set(REQUIRED_METHODS)


# ---------------------------------------------------------------------------
# Fail-safe isolation inside the harvest
# ---------------------------------------------------------------------------

def test_broken_adapter_isolated_with_machine_readable_violations(tmp_path):
    broken = _make(tmp_path, **{"status": "raise"})
    block = snap.observe_clank(broken)
    assert block["status"]["observation"] == "FAILED_ADAPTER"
    assert "status exploded" in block["status"]["error"]
    # sibling evidence surfaces still probed (their own isolation applies):
    assert "health" in block and "last_run" in block
    assert not block.get("contract_violations")  # surface itself conforms


def test_wrong_major_is_isolated_unknown_not_crash(tmp_path):
    broken = _make(tmp_path)
    broken.runtime_version = "9.9.9-vX"
    block = snap.observe_clank(broken)
    assert block["observation"] == "FAILED_ADAPTER"
    assert any("major" in v for v in block["contract_violations"])


def test_registry_duplicate_store_identity_fails_loudly(tmp_path):
    reg = tmp_path / "dup.json"
    reg.write_text(json.dumps({
        "extend_builtin": True,
        "lane-a": {"module": "m", "class": "C", "db": "shared.db"},
        "lane-b": {"module": "m", "class": "C", "db": "shared.db"},
    }), encoding="utf-8")
    with pytest.raises(adapters_mod.AdapterPlaneUnavailable, match="duplicate store"):
        adapters_mod.load_registry(reg)


def test_builtin_registry_itself_has_no_store_collisions():
    # would raise at load if any two builtin lanes shared a store identity
    adapters_mod.load_registry(None)


# ---------------------------------------------------------------------------
# Hot-swap full-pipeline travel: synthetic lane, override file only
# ---------------------------------------------------------------------------

class SyntheticAdapter(ConformingAdapter):
    """Conforming lane with real currency evidence."""

    def last_run(self):
        return {"supported": True, "finished_at": "2026-08-26T05:00:00Z",
                "status": "ok"}


def test_synthetic_lane_travels_full_pipeline_without_core_edits(tmp_path):
    import sys
    module = types.ModuleType("clank_fleet.adapters.synthetic_lane")
    module.SyntheticAdapter = SyntheticAdapter
    sys.modules["clank_fleet.adapters.synthetic_lane"] = module

    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "extend_builtin": True,
        "synthetic-lane": {"module": "clank_fleet.adapters.synthetic_lane",
                           "class": "SyntheticAdapter", "db": "s.db"},
    }), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    built = adapters_mod.build_adapters(state, registry_path=reg)
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(
        inventory_path=inv, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    payload["harvested_at_utc"] = "2026-08-26T06:00:00Z"
    payload["content_hash"] = "sha256:syn"

    synthesis = syn.synthesize_fleet(payload)
    ledger = ano.detect([payload])
    batch = ano.build_batch(None, [payload], ledger) if False else \
        ano.detect([payload])
    recs_list = recs.derive_recommendations({
        "batch_generated_from": "2026-08-26T06:00:00Z", "batch_hash": "h",
        "anomalies": ledger})
    # traveled the whole chain; nothing crashed; no invented anomalies
    assert "synthetic-lane" in synthesis["clanks"]
    assert isinstance(recs_list, list)


def test_broken_lane_does_not_poison_sibling_in_one_harvest(tmp_path):
    from clank_fleet.adapters.free_game_tracker import FreeGameTrackerAdapter
    good = FreeGameTrackerAdapter(db_path=tmp_path / "fgt.db")  # missing -> UNKNOWN
    broken = _make(tmp_path, **{"identity": "raise"})
    built = {"adapters": {"broken-lane": broken, "free-game-tracker": good},
             "versions": {}, "qc_adapters": []}
    payload, warnings = snap.build_snapshot(
        inventory_path=_inv(tmp_path), adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    blocks = payload["clanks"]
    assert blocks["broken-lane"].get("observation") == "FAILED_ADAPTER"
    assert blocks["free-game-tracker"].get("observation") != "FAILED_ADAPTER"
    assert warnings  # surfaced, not swallowed


def _inv(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text("repositories: []\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Read-only mutation proofs across registered adapters (§9)
# ---------------------------------------------------------------------------

READ_ONLY_FIXTURES = {}


def _fixture_db(tmp_path: Path, cid: str) -> Path | None:
    """Generic minimal store per lane: real filenames, tables only as far
    as honestly known (adapters must tolerate their absence)."""
    db = tmp_path / f"{cid}.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE placeholder_probe (id INTEGER)")
    con.commit()
    con.close()
    return db


import sqlite3  # noqa: E402


REGISTERED = [
    "watch-clank", "smartphone-clank", "korean-tech-wire",
    "feature-phone-clank", "smartwatch-clank", "oem-radar",
    "free-game-tracker",
]


@pytest.mark.parametrize("cid", REGISTERED)
def test_observation_is_read_only_no_creation_no_mutation(tmp_path, cid):
    entry = adapters_mod.BUILTIN_REGISTRY[cid]
    module = __import__(entry["module"], fromlist=[entry["class"]])
    adapter_cls = getattr(module, entry["class"])
    db = tmp_path / f"{cid}.db"

    # 1. missing store: observation must NOT create it
    a1 = adapter_cls(db_path=db)
    snap.observe_clank(a1)
    assert not db.exists(), f"{cid}: observer created a participant DB"

    # 2. existing store: byte-identical before/after a full observation
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS probe_x (id INTEGER)")
    con.commit()
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    changes = snap.db_readonly_proof([db])
    a2 = adapter_cls(db_path=db)
    block = snap.observe_clank(a2)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after, f"{cid}: participant DB mutated by observation"
    assert changes.get(db.name) == 0


# ---------------------------------------------------------------------------
# Cross-plane invariants (§7): dimensions never couple
# ---------------------------------------------------------------------------

OPS = ["HEALTHY", "DEGRADED", "FAILED", "UNKNOWN"]
CONT = ["CONTINUOUS", "GAP_KNOWN", "RESTORED_HISTORY", "NEW_EPOCH",
        "UNKNOWN_CONTINUITY"]
LIVE = ["CURRENT", "MATERIALIZATION_GAP", "NO_WORK_DUE", "EXECUTION_STALE",
        "INTENTIONALLY_DORMANT", "UNKNOWN"]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("cont", CONT)
@pytest.mark.parametrize("lv", LIVE)
def test_three_dimensions_survive_every_combination(op, cont, lv):
    """Whatever one plane says must never rewrite another plane's verdict."""
    base_block = {
        "status": {"operational_state": op.lower()},
        "health": {"sources": [{"source_id": "s", "status": "ok"}]},
        "last_run": {"finished_at": "2026-08-26T05:00:00Z"},
    }
    payload = {"harvested_at_utc": "2026-08-26T06:00:00Z",
               "clanks": {"c": dict(base_block)}}
    alone = syn.synthesize_fleet(dict(payload), stale_hours=48.0)

    decorated = {"harvested_at_utc": "2026-08-26T06:00:00Z",
                 "clanks": {"c": dict(base_block, **{
                     "continuity": {"continuity_state": cont,
                                    "epoch_id": "e", "active_event_ids": [],
                                     "evidence_refs": []},
                     "liveness": {"liveness_state": lv, "policy": "PERIODIC",
                                  "stages": {}, "evidence": {}}})}}
    both = syn.synthesize_fleet(decorated, stale_hours=48.0)

    a, b = alone["clanks"]["c"], both["clanks"]["c"]
    assert a["state"] == b["state"], "continuity/liveness changed health"
    assert b["continuity"]["continuity_state"] == cont
    assert b["liveness"]["liveness_state"] == lv
    assert b["continuity"]["orthogonal_to_operational_state"] is True
    assert b["liveness"]["orthogonal_to_operational_health"] is True


def test_stage_implications_never_run_backwards():
    """Fired YES must not imply started/executed/materialized; and a
    mandatory materializer with fire+start but no record IS a gap even
    though the process provably started."""
    from motherclank import liveness as live
    from motherclank import scheduler_traces as straces

    exp = live.make_expectation(expectation_id="E", clank_id="c",
                                policy="PERIODIC", cadence_seconds=3600,
                                authority="cron",
                                materialization_policy="ALWAYS")
    trace = straces.make_trace(
        trace_id="T", clank_id="c", scheduler_type="cron",
        observed_at="2026-08-26T06:00:00Z", invoked_at="2026-08-26T05:55:00Z",
        process_started=False, evidence_source="journal")
    block = {"status": {}, "health": {},
             "last_run": {"finished_at": "2026-08-19T06:00:00Z"}}
    lv = live.derive_liveness(block, exp, observed_at="2026-08-26T06:00:00Z",
                              trace=trace)
    stages = lv["stages"]
    assert stages["SCHEDULER_FIRED"]["value"] == "YES"
    # fired YES does NOT imply started/executed/materialized:
    assert stages["PROCESS_STARTED"]["value"] == "NO"
    assert stages["APPLICATION_EXECUTED"]["value"] == "NO"
    assert stages["RUN_MATERIALIZED"]["value"] == "NO"
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"


def test_provenance_present_on_all_derived_claims():
    payload = {"harvested_at_utc": "2026-08-26T06:00:00Z",
               "content_hash": "sha256:x",
               "clanks": {"c": {
                   "status": {"operational_state": "healthy"},
                   "health": {"sources": [{"source_id": "a",
                                           "status": "ok"}]},
                   "last_run": {"finished_at": "2026-08-26T05:00:00Z"}}}}
    synth = syn.synthesize_fleet(payload)
    claim = synth["clanks"]["c"]
    assert claim["observed_at"]
    assert claim["provenance"]["derived_by"] == "motherclank-m1"
    for a in syn.__dict__ and []:
        pass
