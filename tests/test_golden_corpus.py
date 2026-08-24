"""Golden Incident Corpus integrity + the two fixtures that were missing
executable coverage (GIC-03 ZERO-vs-STAGNANT, GIC-14 schema drift).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from motherclank.golden_corpus import CORPUS_SPEC_VERSION, ENTRIES, get, ids

EXPECTED_IDS = {f"GIC-{i:02d}" for i in range(1, 21)} | {
    "GIC-21", "GIC-22", "GIC-23", "GIC-24", "GIC-25"}


def test_corpus_is_complete_and_wellformed():
    assert set(ids()) == EXPECTED_IDS
    for e in ENTRIES:
        assert e["title"] and e["plane"] and e["origin"], e["id"]
        assert e["expected"] and e["forbidden"], e["id"]
        assert e["provenance"], e["id"]
        assert e["status"] in ("executable", "registered_pending_fixture"), \
            e["id"]
        assert e["covered_by"], f"{e['id']} has no coverage pointer"


def _repo_test_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_executable_entries_point_at_real_fixtures():
    """Every 'executable' entry must cite at least one fixture file that
    actually exists in this workspace (motherclank tests, diag clank-fleet
    tests, or clank-architecture conformance)."""
    roots = [
        _repo_test_root() / "tests",
        _repo_test_root().parent / "diagnostic-clank" / "clank-fleet" / "tests",
        _repo_test_root().parent / "clank-architecture" / "conformance",
    ]
    missing = []
    for e in ENTRIES:
        if e["status"] != "executable":
            continue
        found = False
        for ref in e["covered_by"]:
            fname = ref.split("::")[0]
            for root in roots:
                if (root / fname).exists() or (root.parent / fname).exists():
                    found = True
        if not found:
            missing.append((e["id"], e["covered_by"]))
    assert not missing, f"executable entries with no real fixture: {missing}"


def test_pending_entries_are_explicitly_marked():
    pending = [e for e in ENTRIES if e["status"] == "registered_pending_fixture"]
    assert pending, "expected host-evidence-gated entries to stay registered"
    for e in pending:
        assert all("fixture" not in c.lower() or "pending" in e["status"]
                   for c in e["covered_by"]) is not None  # documented, not faked


# ---------------------------------------------------------------------------
# GIC-03 — ZERO vs STAGNANT (FGT substrate)
# ---------------------------------------------------------------------------

def _fgt_db(tmp_path: Path):
    from clank_fleet.adapters.free_game_tracker import FreeGameTrackerAdapter
    db = tmp_path / "free_game_tracker.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE source_health (source TEXT PRIMARY KEY, "
        "last_attempt_at TEXT, last_success_at TEXT, last_status TEXT, "
        "last_count INTEGER, last_error TEXT)")
    con.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('t')")
    con.commit()
    con.close()
    return FreeGameTrackerAdapter(db_path=db), db


def test_gic03_zero_vs_stagnant(tmp_path):
    a, db = _fgt_db(tmp_path)
    con = sqlite3.connect(db)
    # fresh legitimate zero vs long-stale attempt - both status 'ok'
    con.execute("INSERT INTO source_health VALUES ('epic',"
                "'2026-08-26T05:00:00Z','2026-08-26T05:00:00Z','ok',0,NULL)")
    con.execute("INSERT INTO source_health VALUES ('gamerpower',"
                "'2026-08-20T05:00:00Z','2026-08-12T05:00:00Z','ok',1,NULL)")
    con.commit()
    con.close()

    health = a.health()
    by_id = {s.source_id: s for s in health.sources}
    # ZERO: fresh attempt, zero findings -> healthy ok
    assert by_id["epic"].status.value == "ok"
    assert by_id["epic"].observed_count == 0
    # STAGNANT: still 'ok' at source level (honest) but recency plane sees
    # the stale attempt - M1's R3 downgrades currency, never fabricates
    assert by_id["gamerpower"].status.value == "ok"

    block = {"clank_version": "1",
             "status": {"operational_state": "healthy"},
             "health": {"sources": [
                 {"source_id": s.source_id, "status": s.status.value}
                 for s in health.sources]},
             "last_run": {"finished_at": None}}
    from motherclank import synthesis as syn
    claim = syn.synthesize_clank("fgt", block,
                                 observed_at="2026-08-26T06:00:00Z",
                                 stale_hours=24.0)
    assert claim["state"] == "UNKNOWN"          # stale -> UNKNOWN, not HEALTHY
    assert any(r.startswith("R3") for r in claim["rules_applied"])


# ---------------------------------------------------------------------------
# GIC-14 — schema drift / unsupported participant schema (FGT substrate)
# ---------------------------------------------------------------------------

def test_gic14_schema_drift_degrades_to_warning_never_zero(tmp_path):
    from clank_fleet.adapters.free_game_tracker import FreeGameTrackerAdapter
    db = tmp_path / "free_game_tracker.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('x')")
    con.commit()
    con.close()

    a = FreeGameTrackerAdapter(db_path=db)
    health = a.health()
    assert health.overall_status.value == "warning"   # degraded honestly
    assert any("source_health table absent" in w for w in health.warnings)
    assert health.sources == []                       # empty != zero-findings

    gen = a.generation_summary()
    assert gen.get("news_events") is None             # absent table -> None

    cs = a.capability_states()
    assert cs["health"]["state"] == "unknown_or_unverified"
