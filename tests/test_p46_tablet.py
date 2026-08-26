"""P-4.6 Tablet onboarding goldens - the tenth lane, intentionally RETIRED.

Fixture built from schema evidence in canonical tablet_clank source
(storage/db.py CREATE TABLE statements).

Key invariants:
- RETIRED lifecycle produces INTENTIONALLY_DORMANT liveness, not STALE_RUN
- materialization_policy ALWAYS respects native collector_runs
- schema_migrations provides real version evidence
- no delivery/QC fabricated
- missing store -> UNKNOWN, never creation
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import snapshot as snap
from motherclank import synthesis as syn
from motherclank import liveness as live


def _tablet_fixture(tmp_path: Path):
    db = tmp_path / "tablet_clank.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                " applied_at TEXT NOT NULL)")
    con.execute("CREATE TABLE collector_runs (id INTEGER PRIMARY KEY,"
                " source_id TEXT NOT NULL, started_at TEXT NOT NULL,"
                " finished_at TEXT, status TEXT NOT NULL,"
                " raw_count INTEGER DEFAULT 0,"
                " validated_count INTEGER DEFAULT 0,"
                " rejected_count INTEGER DEFAULT 0,"
                " accepted_count INTEGER DEFAULT 0,"
                " new_count INTEGER DEFAULT 0, updated_count INTEGER DEFAULT 0,"
                " resighted_count INTEGER DEFAULT 0, error TEXT)")
    con.execute("CREATE TABLE sources (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO sources VALUES ('honor_de')")
    con.execute("INSERT INTO collector_runs VALUES (1,'honor_de',"
                " '2026-08-15T00:00:00Z','2026-08-15T00:05:00Z','ok',"
                " 5,5,0,3,2,0,1,NULL)")
    con.execute("INSERT INTO schema_migrations VALUES (3, '2026-08-10')")
    con.commit(); con.close()
    return db


def _adapter(db_path):
    from clank_fleet.adapters.tablet_clank import TabletClankAdapter
    return TabletClankAdapter(db_path=db_path)


def test_tablet_registered_via_registry():
    entry = adapters_mod.BUILTIN_REGISTRY["tablet-clank"]
    assert entry["class"] == "TabletClankAdapter"


def test_tablet_core_ignorance():
    import re
    core = Path(adapters_mod.__file__).parent

    def code_only(text):
        text = re.sub(r'"""[\s\S]*?"""', ' ', text)
        return "\n".join(re.sub(r"#.*$", "", l) for l in text.splitlines())

    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "cli"):
        code = code_only((core / f"{m}.py").read_text(encoding="utf-8"))
        assert "tablet" not in code.lower(), f"{m}.py hardcodes tablet"


def test_retired_lifecycle_produces_dormant_liveness(tmp_path):
    """GIC-10 pattern: RETIRED lane with old runs produces no STALE_RUN."""
    exp = live.make_expectation(
        expectation_id="EXP-tablet", clank_id="tablet-clank",
        policy="RETIRED", authority="none", active=True,
        notes="finite soak completed; manual/on-demand production")
    block = {"status": {"operational_state": "healthy"},
             "health": {"sources": [{"source_id": "h", "status": "ok"}]},
             "last_run": {"finished_at": "2026-08-01T00:00:00Z"},
             "_synthesis_rules": ["R3"]}
    lv = live.derive_liveness(block, exp, observed_at="2026-08-27T06:00:00Z")
    assert lv["liveness_state"] == "INTENTIONALLY_DORMANT"
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "NOT_APPLICABLE"


def test_schema_revision_native(tmp_path):
    a = _adapter(_tablet_fixture(tmp_path))
    assert a.schema_revision() == 3  # MAX(version) from schema_migrations


def test_capability_states_canonical(tmp_path):
    a = _adapter(_tablet_fixture(tmp_path))
    cs = a.capability_states()
    from clank_runtime.contracts.capabilities import validate_capability_states
    assert validate_capability_states(cs) == []
    assert cs["delivery"]["state"] == "unsupported_by_policy"
    assert cs["scheduler_trace"]["state"] == "unsupported_by_policy"


def test_read_only_missing_store_never_created(tmp_path):
    missing = tmp_path / "never.db"
    a = _adapter(missing)
    snap.observe_clank(a)
    assert not missing.exists()


def test_read_only_existing_store_byte_identical(tmp_path):
    a = _adapter(_tablet_fixture(tmp_path))
    before = Path(a.db_path).read_bytes()
    snap.observe_clank(a)
    assert before == Path(a.db_path).read_bytes()


def test_full_pipeline_travel(tmp_path):
    a = _adapter(_tablet_fixture(tmp_path))
    built = {"adapters": {"tablet-clank": a},
             "versions": {"adapter_contract_version": "t"},
             "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(
        inventory_path=inv, adapters_result=built,
        real_state_dir=tmp_path, out_dir=tmp_path)
    payload["harvested_at_utc"] = "2026-08-27T06:00:00Z"
    payload["content_hash"] = "sha256:tablet-travel"

    synth = syn.synthesize_fleet(payload, stale_hours=99999)
    claim = synth["clanks"]["tablet-clank"]
    assert claim["capability_states"]["collection"]["state"] == "active"
    # RETIRED lane should not produce health-based alarm
    assert claim["state"] in ("HEALTHY", "UNKNOWN")


def test_ten_lanes_registered():
    registry_keys = set(adapters_mod.BUILTIN_REGISTRY.keys())
    assert len(registry_keys) == 10
    assert "tablet-clank" in registry_keys
