"""FGT onboarding goldens - hermetic fixtures built strictly from the
canonical newsroom schema (news_events, source_health, new_releases,
steam_deals, alembic_version).

REAL_STATE_VALIDATION = BLOCKED in the authoring environment; Claude
validates against a live read-only copy per OPERATOR_HANDOFF.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import snapshot as snap

CANONICAL_STATES = {"active", "supported_unconfigured", "supported_undeployed",
                    "unsupported_by_policy", "unsupported",
                    "unknown_or_unverified"}


def _fgt_fixture(tmp_path: Path):
    from clank_fleet.adapters.free_game_tracker import FreeGameTrackerAdapter
    db = tmp_path / "free_game_tracker.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE source_health (source TEXT PRIMARY KEY, "
        "last_attempt_at TEXT, last_success_at TEXT, last_status TEXT, "
        "last_count INTEGER, last_error TEXT)")
    con.execute(
        "CREATE TABLE news_events (id INTEGER PRIMARY KEY, event_key TEXT)")
    con.execute(
        "CREATE TABLE new_releases (appid INTEGER PRIMARY KEY)")
    con.execute(
        "CREATE TABLE steam_deals (appid INTEGER PRIMARY KEY)")
    con.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('deadbee1234')")
    return db, con


def _seed_sources(con: sqlite3.Connection,
                  rows: list[tuple[str, str, str | None, int, str | None]]):
    """rows: (source, last_status, last_error, last_count, attempt_time)."""
    for source, status, error, count, ts in rows:
        con.execute(
            "INSERT INTO source_health VALUES (?,?,?,?,?,?)",
            (source, ts, ts if status == "ok" else None, status, count, error))
    con.commit()


# ---------------------------------------------------------------------------
# FGT-G1/G2/G3 - per-source outcomes
# ---------------------------------------------------------------------------

def test_fgt_g1_successful_attempt_zero_findings_is_ok(tmp_path):
    db, con = _fgt_fixture(tmp_path)
    _seed_sources(con, [("epic", "ok", None, 0, "2026-08-25T05:00:00Z")])
    health = _adapter(db).health()
    assert health.overall_status.value == "healthy"   # quiet week != failure
    entry = health.sources[0]
    assert entry.status.value == "ok" and entry.observed_count == 0


def test_fgt_g2_successful_attempt_with_findings(tmp_path):
    db, con = _fgt_fixture(tmp_path)
    _seed_sources(con, [("epic", "ok", None, 7, "2026-08-25T05:00:00Z")])
    entry = _adapter(db).health().sources[0]
    assert entry.observed_count == 7 and entry.status.value == "ok"


def test_fgt_g3_source_failure_preserves_error(tmp_path):
    db, con = _fgt_fixture(tmp_path)
    _seed_sources(con, [("epic", "error", "HTTP 403", 0,
                         "2026-08-25T05:00:00Z"),
                        ("gog", "ok", None, 2, "2026-08-25T05:01:00Z")])
    health = _adapter(db).health()
    assert health.overall_status.value == "degraded"
    epic = next(s for s in health.sources if s.source_id == "epic")
    assert epic.health_reason == "HTTP 403"


# ---------------------------------------------------------------------------
# FGT-G4 delivery independence; G5 missing evidence -> UNKNOWN
# ---------------------------------------------------------------------------

def test_fgt_g4_delivery_claims_never_borrow_from_generation():
    a = _adapter(_fresh_db())
    gen = a.generation_summary()
    delivery = a.delivery_summary()
    assert gen["available"] and gen.get("news_events", 0) >= 0
    # DB-level delivery is unsupported: outcomes are log-only in FGT
    assert delivery["supported"] is False
    cs = a.capability_states()
    assert cs["delivery"]["state"] == "unsupported"
    assert cs["events"]["state"] in CANONICAL_STATES


def test_fgt_g5_missing_store_stays_unknown_everywhere(tmp_path):
    a = _adapter(tmp_path / "missing.db")
    assert a.health().overall_status.value == "unknown"
    lr = a.last_run()
    assert lr["supported"] is False
    cs = a.capability_states()
    assert cs["collection"]["state"] == "unknown_or_unverified"
    assert cs["delivery"]["state"] == "unsupported"      # absence established


# ---------------------------------------------------------------------------
# FGT-G6 materialization policy behavior (P-4.1 integration)
# ---------------------------------------------------------------------------

def test_fgt_g6_execution_currency_derived_and_labeled(tmp_path):
    db, con = _fgt_fixture(tmp_path)
    _seed_sources(con, [("epic", "ok", None, 0, "2026-08-25T05:00:00Z"),
                        ("gog", "ok", None, 3, "2026-08-25T04:30:00Z")])
    a = _adapter(db)
    lr = a.last_run()
    assert lr["supported"] is True
    assert lr["finished_at"] == "2026-08-25T05:00:00Z"  # newest attempt
    assert lr["derived_from"] == "MAX(source_health.last_attempt_at)"


def _fresh_db() -> Path:
    import tempfile
    db, _ = _fgt_fixture(Path(tempfile.mkdtemp()))
    return db


def _adapter(db: Path):
    from clank_fleet.adapters.free_game_tracker import FreeGameTrackerAdapter
    return FreeGameTrackerAdapter(db_path=db)


# ---------------------------------------------------------------------------
# FGT-G9 registry-only onboarding / no core special case
# ---------------------------------------------------------------------------

def test_fgt_g9_onboarded_via_registry_without_core_logic():
    assert adapters_mod.BUILTIN_REGISTRY["free-game-tracker"]["class"] == \
        "FreeGameTrackerAdapter"
    core = Path(adapters_mod.__file__).parent
    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "qc_corpus",
              "soak", "drift", "report", "inbox_bridge", "registry_shim",
              "cli"):
        code = (core / f"{m}.py").read_text(encoding="utf-8")
        assert "free-game-tracker" not in code and "free_game_tracker" not \
            in code, f"{m}.py hardcodes FGT"


# ---------------------------------------------------------------------------
# FGT-G10 + fleet-wide capability contract guarantee
# ---------------------------------------------------------------------------

def test_fgt_g10_only_canonical_capability_states(tmp_path):
    db, _ = _fgt_fixture(tmp_path)
    cs = _adapter(db).capability_states()
    assert cs, "adapter must declare capability states"
    for domain, statement in cs.items():
        assert statement["state"] in CANONICAL_STATES, domain
        assert statement.get("evidence"), domain


def test_every_registered_adapter_emits_canonical_capability_states(tmp_path):
    """Fleet-level mechanical guarantee: any lane onboarded through the
    registry MUST implement capability_states() with canonical values.
    New adapters fail this test until they conform."""
    from clank_runtime.contracts.capabilities import \
        validate_capability_states
    failures = {}
    for cid, entry in adapters_mod.BUILTIN_REGISTRY.items():
        module = __import__(entry["module"], fromlist=[entry["class"]])
        adapter_cls = getattr(module, entry["class"])
        if not hasattr(adapter_cls, "capability_states"):
            failures[cid] = "missing capability_states()"
            continue
        probe = tmp_path / f"{cid}.db"
        instance = adapter_cls(db_path=probe)
        violations = validate_capability_states(instance.capability_states())
        if violations:
            failures[cid] = violations
    assert not failures, failures


def test_snapshot_carries_capability_blocks_for_all_lanes(tmp_path):
    built = adapters_mod.build_adapters(tmp_path)
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, _ = snap.build_snapshot(inventory_path=inv,
                                     adapters_result=built,
                                     real_state_dir=tmp_path,
                                     out_dir=tmp_path)
    for cid, block in payload["clanks"].items():
        cs = block.get("capability_states")
        assert cs, f"{cid}: snapshot lacks capability_states"
        assert not block.get("capability_states_violations"), cid
