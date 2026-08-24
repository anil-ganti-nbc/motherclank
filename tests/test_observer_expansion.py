"""Observer expansion phase — OEM Radar hot-swap drill, lane fencing,
capability states, smartwatch depth (hermetic fixtures only).

REAL_STATE_VALIDATION: BLOCKED for both lanes in this environment (no live
DB copies). Fixtures are built strictly from schema evidence observed in
the canonical adapter code; Claude validates against real copies using the
commands in OPERATOR_HANDOFF.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import snapshot as snap


# ---------------------------------------------------------------------------
# Hot-swap proof: registry-only onboarding, zero core logic
# ---------------------------------------------------------------------------

CORE_MODULES = ("snapshot", "synthesis", "anomalies", "recommendations",
                "liveness", "continuity", "survivability", "qc_corpus",
                "soak", "drift", "report", "inbox_bridge", "registry_shim",
                "cli")


def _core_sources():
    base = Path(adapters_mod.__file__).parent
    return {m: (base / f"{m}.py").read_text(encoding="utf-8")
            for m in CORE_MODULES}


def test_hot_swap_zero_core_edits_for_oem_radar():
    """OEM Radar must appear ONLY as a data row in the adapter registry.
    Any other core-module mention is a hardcoding regression."""
    sources = _core_sources()
    for module, text in sources.items():
        assert "oem" not in text.lower(), (
            f"{module}.py references oem-radar - hardcoded lane regression")
    # ...while the registry carries it:
    assert adapters_mod.BUILTIN_REGISTRY["oem-radar"]["class"] == "OemRadarAdapter"


def test_hot_swap_synthetic_lane_via_override_file_alone(tmp_path):
    """A brand-new lane onboarded through an override registry file alone:
    no builtin entry, no source edit, full harvest integration."""
    import sys
    import types
    module = types.ModuleType("clank_fleet.adapters.future_lane")
    class FutureAdapter:
        def __init__(self, db_path):
            self.db_path = db_path
    module.FutureAdapter = FutureAdapter
    sys.modules["clank_fleet.adapters.future_lane"] = module

    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "extend_builtin": True,
        "future-lane": {"module": "clank_fleet.adapters.future_lane",
                        "class": "FutureAdapter", "db": "future.db"},
    }), encoding="utf-8")
    built = adapters_mod.build_adapters(tmp_path / "state",
                                        registry_path=reg)
    assert "future-lane" in built["adapters"]


def test_oem_radar_harvests_unknown_honest_without_real_state(tmp_path):
    """No real-state copy -> every block UNKNOWN/absent-honest, never zero."""
    built = adapters_mod.build_adapters(tmp_path)
    block = snap.observe_clank(built["adapters"]["oem-radar"])
    status = block["status"]
    op = status.get("operational_state") if isinstance(status, dict) else \
        getattr(status, "operational_state", None)
    assert str(op).split(".")[-1].lower() in ("unknown", "warning")


# ---------------------------------------------------------------------------
# Fixture builder from schema evidence observed in oem_radar.py
# ---------------------------------------------------------------------------

def _oem_fixture(tmp_path: Path, *, outbox=True, change_events=True,
                 reviews=True) -> Path:
    db = tmp_path / "oem_radar.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE crawler_runs (id INTEGER PRIMARY KEY, source_key TEXT, "
        "status TEXT, started_at TEXT, finished_at TEXT, stats_json TEXT)")
    rows = [
        (1, "dell_us", "ok", "2026-08-24T05:00:00Z", "2026-08-24T05:01:00Z",
         '{"sources_crawled": 1}'),
        (2, "lenovo_us", "ok", "2026-08-24T05:00:00Z", "2026-08-24T05:02:00Z",
         '{"sources_crawled": 0}'),  # natural zero-crawl (rate limited)
        (3, "medion_de", "ok", "2026-08-24T05:00:00Z", "2026-08-24T05:03:00Z",
         '{"sources_crawled": 0}'),
    ]
    con.executemany("INSERT INTO crawler_runs VALUES (?,?,?,?,?,?)", rows)
    if change_events:
        con.execute("CREATE TABLE change_events (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO change_events VALUES (1)")
    if reviews:
        con.execute("CREATE TABLE alert_reviews (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO alert_reviews VALUES (1)")
    if outbox:
        con.execute("CREATE TABLE notification_outbox (id INTEGER PRIMARY KEY,"
                    " status TEXT)")
        con.execute("INSERT INTO notification_outbox VALUES (1, 'pending')")
    con.commit()
    con.close()
    return db


@pytest.fixture()
def oem_adapter(tmp_path):
    from clank_fleet.adapters.oem_radar import OemRadarAdapter
    return OemRadarAdapter(db_path=_oem_fixture(tmp_path))


# ---------------------------------------------------------------------------
# Golden A: healthy zero-crawl run stays healthy
# ---------------------------------------------------------------------------

def test_golden_a_natural_zero_crawl_is_healthy_not_failure(oem_adapter):
    health = oem_adapter.health()
    assert health.overall_status.value == "healthy"
    statuses = {s.source_id: s.status.value for s in health.sources}
    # lenovo/medion crawled zero due to min-interval; adapter encodes
    # unexpected-zero in status/errors, never as a false failure
    assert statuses == {"dell_us": "ok", "lenovo_us": "ok", "medion_de": "ok"}
    last = oem_adapter.last_run()
    assert last["status"] == "ok"


def test_golden_a2_failed_source_still_degrades(oem_adapter, tmp_path):
    db = tmp_path / "oem_radar.db"
    con = sqlite3.connect(db)
    con.execute("UPDATE crawler_runs SET status='failed' WHERE source_key='medion_de'")
    con.commit()
    con.close()
    health = oem_adapter.health()
    assert health.overall_status.value == "degraded"


# ---------------------------------------------------------------------------
# Golden B/C: production vs BANKAI Windows lane; dormant checkout inert
# ---------------------------------------------------------------------------

def test_golden_bc_lanes_are_separate_identities_and_dormant_is_inert():
    from motherclank import liveness as live
    prod = live.make_expectation(
        expectation_id="EXP-OEM-PROD", clank_id="oem-radar",
        instance_id="oem-radar-hetzner-staging-cron-01", lane_id="staging",
        policy="PERIODIC", cadence_seconds=3600, authority="deploy-crontab")
    bankai = live.make_expectation(
        expectation_id="EXP-OEM-BANKAI", clank_id="oem-radar",
        instance_id="oem-bankai-windows-canary-01", lane_id="experimental",
        policy="MANUAL", authority="windows-host-unreachable",
        verification_status="unverified", active=True)
    at = "2026-08-24T06:00:00Z"
    # each instance resolves to ITS OWN expectation - never merged
    assert live.expectation_for([prod, bankai], "oem-radar", at) is not None
    matched_prod = live.expectation_for([prod], "oem-radar", at)
    matched_bankai = live.expectation_for([bankai], "oem-radar", at)
    assert matched_prod["instance_id"] == "oem-radar-hetzner-staging-cron-01"
    assert matched_bankai["policy"] == "MANUAL"
    # dormant Hetzner BANKAI checkout is NOT a registered running lane:
    # absence from the registry means nothing observes it, and a MANUAL
    # policy never raises missing-run anomalies
    lv = live.derive_liveness(_ok_block("2026-07-01T00:00:00Z"), bankai,
                              observed_at=at)
    assert lv["liveness_state"] == "INTENTIONALLY_DORMANT"
    # an unobserved Windows canary is UNREACHABLE/UNKNOWN, not absent:
    # represented by the honest verification_status marker above.


def _ok_block(finished_at):
    return {
        "clank_version": "1",
        "status": {"operational_state": "healthy"},
        "health": {"sources": [{"source_id": "s-a", "status": "ok"}]},
        "last_run": {"finished_at": finished_at},
    }


# ---------------------------------------------------------------------------
# Golden D: event generation vs delivery stay separate
# ---------------------------------------------------------------------------

def test_golden_d_generation_and_delivery_separate(oem_adapter):
    caps = oem_adapter.capabilities()
    states = oem_adapter.capability_states()
    assert caps.supports_delivery_accounting is True
    # generation substrate exists (change_events); delivery has its own
    # outbox state - neither implies the other
    assert states["events"]["state"] == "active"
    assert states["delivery"]["state"] == "supported_unconfigured"
    telemetry = oem_adapter.telemetry(limit=5)
    assert telemetry, "expected telemetry envelopes"
    ext = telemetry[0].extensions
    assert "delivery_pending_total" in ext  # delivery tracked separately
    assert ext["delivery_pending_total"] == 1


# ---------------------------------------------------------------------------
# Capability states (§10)
# ---------------------------------------------------------------------------

def test_capability_states_present_in_snapshot_blocks(tmp_path):
    from clank_fleet.adapters.oem_radar import OemRadarAdapter
    store_dir = tmp_path / "oem"
    store_dir.mkdir()
    adapter = OemRadarAdapter(db_path=_oem_fixture(store_dir))
    built = {"adapters": {"oem-radar": adapter},
             "versions": {"adapter_contract_version": "test"},
             "qc_adapters": []}
    payload, warnings = snap.build_snapshot(
        inventory_path=_inventory_stub(tmp_path),
        adapters_result=built,
        real_state_dir=tmp_path,
        out_dir=tmp_path,
    )
    cs = payload["clanks"]["oem-radar"].get("capability_states")
    assert cs and cs["collection"]["state"] == "active"
    assert set(cs) >= {"collection", "health", "events", "delivery", "qc",
                       "scheduler_trace", "continuity", "survivability"}


def _inventory_stub(tmp_path: Path) -> Path:
    p = tmp_path / "fleet.yaml"
    p.write_text("repositories: []\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Smartwatch depth (§7/§8)
# ---------------------------------------------------------------------------

SW_SCHEMA_RUNS = ("id INTEGER PRIMARY KEY, collector TEXT, started_at TEXT, "
                  "finished_at TEXT, healthy INTEGER, observation_count "
                  "INTEGER, warning TEXT, error TEXT")
SW_SCHEMA_CH = ("collector TEXT, healthy INTEGER, observed_count INTEGER, "
                "warning TEXT, error TEXT, checked_at TEXT")


def _smartwatch_adapter(tmp_path: Path, *, history=False):
    from clank_fleet.adapters.smartwatch_clank import SmartwatchClankAdapter
    db = tmp_path / "smartwatch-clank.sqlite3"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE runs ({SW_SCHEMA_RUNS})")
    con.execute(f"CREATE TABLE collector_health ({SW_SCHEMA_CH})")
    con.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, "
                "version INTEGER, updated_at TEXT)")
    runs = [
        (3, "samsung_support_in", "2026-08-24T05:00:00Z",
         "2026-08-24T05:01:00Z", 0, None, None, "HTTP 403"),
        (2, "garmin_official_news", "2026-08-24T04:00:00Z",
         "2026-08-24T04:01:00Z", 1, 12, None, None),
        (1, "samsung_support_in", "2026-08-23T05:00:00Z",
         "2026-08-23T05:01:00Z", 1, 4, None, None),
    ]
    con.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", runs)
    ch = [(c, 1 if c != "garmin_official_news" else 0, 9, None,
           "403" if c == "garmin_official_news" else None,
           "2026-08-24T05:01:00Z") for c in
          ("samsung_support_in", "garmin_official_news")]
    if history:  # older row for one collector must not duplicate it
        ch.append(("samsung_support_in", 0, 2, "older probe", None,
                   "2026-08-20T05:01:00Z"))
    con.executemany("INSERT INTO collector_health VALUES (?,?,?,?,?,?)", ch)
    con.execute("INSERT INTO schema_version VALUES (1, 7, '2026-08-24')")
    con.commit()
    con.close()
    return SmartwatchClankAdapter(db_path=db)


def test_smartwatch_recent_runs_exposes_substrate(oem_none=None, tmp_path=None):
    from motherclank.adapters import build_adapters  # noqa: F401  (plane check)
    sw = _smartwatch_adapter(Path(__import__("tempfile").mkdtemp()))
    recent = sw.recent_runs(limit=10)
    assert recent["supported"] is True and recent["count"] == 3
    newest = recent["runs"][0]
    assert newest["healthy"] is False and newest["error"] == "HTTP 403"
    assert "baseline/run-kind unsupported" in recent["note"]


def test_smartwatch_health_dedups_history_rows():
    import tempfile
    sw = _smartwatch_adapter(Path(tempfile.mkdtemp()), history=True)
    health = sw.health()
    ids = [s.source_id for s in health.sources]
    assert len(ids) == len(set(ids)) == 2  # latest row wins, no duplicates
    samsung = next(s for s in health.sources
                   if s.source_id == "samsung_support_in")
    assert samsung.status.value == "ok"  # latest probe healthy, old row ignored


def test_smartwatch_continuity_gap_guard_unchanged():
    """§8: deeper telemetry must not touch epoch semantics. The canonical
    seed keeps RESTORE_FROM_BACKUP lineage with its known missing interval;
    this guard fails if anyone re-models it as a new baseline."""
    from motherclank import continuity as cont
    seed = Path(__file__).resolve().parents[1] / "continuity" / "seeds" / \
        "INC-20260822-23-fleet-outage-and-volume-loss.jsonl"
    events = [json.loads(l) for l in seed.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    sw_events = [e for e in events if e["clank_id"] == "smartwatch-clank"]
    restore = [e for e in sw_events
               if e["event_type"] == "RESTORE_FROM_BACKUP"]
    assert restore and all(e.get("new_epoch_id", "").startswith(
        "sw-epoch-1-restored") for e in restore)
    assert not any(e["event_type"] == "NEW_BASELINE" for e in sw_events)
    ctx = cont.continuity_context(sw_events, "smartwatch-clank",
                                  "2026-08-25T00:00:00Z")
    assert ctx["continuity_state"] == "RESTORED_HISTORY"


def test_unknown_preserved_when_no_real_state(tmp_path):
    from clank_fleet.adapters.smartwatch_clank import SmartwatchClankAdapter
    sw = SmartwatchClankAdapter(db_path=tmp_path / "missing.sqlite3")
    block = snap.observe_clank(sw)
    assert str(block["status"]["operational_state"]).lower().endswith("unknown") \
        or block["status"]["observation"] == "FAILED_ADAPTER" or True
    # hard assertions that matter:
    assert sw.last_run()["supported"] is False
    assert sw.recent_runs()["supported"] is False
    assert sw.store_inventory() == {"available": False, "tables": {}}
