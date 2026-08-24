"""CTW v0.3 dogfood goldens - the first real registry-driven onboarding of
a genuinely heterogeneous lane (multi-layer NEWS/COMMUNITY/DOCUMENTARY,
native per-attempt run rows, sent-only delivery persistence).

Fixtures built strictly from schema evidence in canonical participant
source (database/models.py @ 9eec9f0).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motherclank import adapters as adapters_mod
from motherclank import snapshot as snap

CANONICAL_STATES = {"active", "supported_unconfigured", "supported_undeployed",
                    "unsupported_by_policy", "unsupported",
                    "unknown_or_unverified"}


def _ctw_fixture(tmp_path: Path, *, layers=("NEWS",), zero_work=False,
                 fail_one=False):
    from clank_fleet.adapters.chinese_tech_wire import ChineseTechWireAdapter
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "chinese_tech_wire.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE source_runs (id INTEGER PRIMARY KEY, source TEXT,"
        " layer TEXT, started_at TEXT, finished_at TEXT, success INTEGER,"
        " articles_found INTEGER DEFAULT 0, articles_new INTEGER DEFAULT 0,"
        " parse_errors INTEGER DEFAULT 0, request_errors INTEGER DEFAULT 0,"
        " response_time_ms INTEGER)")
    con.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY,"
                " article_id INTEGER, sent_at TEXT, discord_message_id TEXT)")
    rows = []
    rid = 0
    for i, layer in enumerate(layers):
        rid += 1
        if zero_work:
            # successful attempt, nothing new: legitimate healthy zero-work
            rows.append((rid, f"src{i}", layer, "2026-08-26T05:00:00Z",
                         "2026-08-26T05:01:00Z", 1, 10, 0, 0, 0, 250))
        else:
            rows.append((rid, f"src{i}", layer, "2026-08-26T05:00:00Z",
                         "2026-08-26T05:01:00Z", 1, 12, 3, 0, 0, 200))
    if fail_one and layers:
        rid += 1
        rows.append((rid, "benchlife", "NEWS", "2026-08-26T05:00:00Z",
                     "2026-08-26T05:02:00Z", 0, 0, 0, 2, 1, None))
    con.executemany("INSERT INTO source_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
    con.execute("INSERT INTO articles VALUES (1)")
    con.execute("INSERT INTO notifications VALUES (1, 1, "
                "'2026-08-26T05:05:00Z', 'dm-1')")
    con.commit()
    con.close()
    return ChineseTechWireAdapter(db_path=db)


def test_ctw_registered_via_registry_row_only():
    entry = adapters_mod.BUILTIN_REGISTRY["chinese-tech-wire"]
    assert entry["class"] == "ChineseTechWireAdapter"
    core = Path(adapters_mod.__file__).parent
    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "qc_corpus",
              "soak", "drift", "report", "inbox_bridge", "registry_shim",
              "cli", "scheduler_traces", "contract"):
        code = (core / f"{m}.py").read_text(encoding="utf-8")
        assert "chinese" not in code.lower() and "ctw" not in code.lower(), \
            f"{m}.py hardcodes CTW"


def test_ctw_layers_stay_distinct(oem_none=None, tmp_path=None):
    a = _ctw_fixture(Path(__import__("tempfile").mkdtemp()),
                     layers=("NEWS", "COMMUNITY"))
    health = a.health()
    ids = [s.source_id for s in health.sources]
    assert "ithome[NEWS]" in ids[0] or len(ids) == len(set(ids))
    assert all("[" in s.source_id for s in health.sources)  # layer tagged


def test_ctw_successful_zero_findings_is_healthy(tmp_path):
    a = _ctw_fixture(tmp_path / "zw", zero_work=True)
    health = a.health()
    assert health.overall_status.value == "healthy"
    for s in health.sources:
        assert s.status.value == "ok"
        assert s.observed_count == 0   # legitimate zero, never failure


def test_ctw_failed_source_degrades_lane(tmp_path):
    a = _ctw_fixture(tmp_path / "f", fail_one=True)
    health = a.health()
    assert health.overall_status.value == "degraded"
    failed = [s for s in health.sources if s.status.value == "failed"]
    assert failed and failed[0].source_id.startswith("benchlife")


def test_ctw_delivery_sent_persisted_suppressed_absent():
    a = _ctw_fixture(Path(__import__("tempfile").mkdtemp()))
    d = a.delivery_summary()
    assert d["supported"] is True
    assert d["sent_total"] == 1
    assert d["suppressed_total"] is None       # log-only: honestly absent
    cs = a.capability_states()
    assert cs["delivery"]["state"] == "active"
    assert "log-only" in cs["delivery"]["evidence"]


def test_ctw_schema_revision_honestly_unknown(tmp_path):
    a = _ctw_fixture(tmp_path / "s")
    assert a.schema_revision() is None         # no version table -> UNKNOWN


def test_ctw_capability_states_canonical(tmp_path):
    a = _ctw_fixture(tmp_path / "c")
    from clank_runtime.contracts.capabilities import \
        validate_capability_states
    cs = a.capability_states()
    assert validate_capability_states(cs) == []
    assert cs["qc"]["state"] == "unsupported"
    assert cs["baseline_run_kind"]["state"] == "unsupported"
    assert cs["continuity"]["state"] == "unknown_or_unverified"  # unproven


def test_ctw_missing_store_is_unknown_never_created(tmp_path):
    missing = tmp_path / "nope.db"
    from clank_fleet.adapters.chinese_tech_wire import ChineseTechWireAdapter
    adapter = ChineseTechWireAdapter(db_path=missing)
    block = snap.observe_clank(adapter)
    status = block["status"]
    op = status.get("operational_state") if isinstance(status, dict) else \
        getattr(status, "operational_state", None)
    assert str(op).split(".")[-1].lower() == "unknown"
    assert not missing.exists()                # never created


def test_ctw_full_pipeline_travel(tmp_path):
    a = _ctw_fixture(tmp_path / "p")
    built = {"adapters": {"chinese-tech-wire": a},
             "versions": {"adapter_contract_version": "t"}, "qc_adapters": []}
    inv = tmp_path / "fleet.yaml"
    inv.write_text("repositories: []\n", encoding="utf-8")
    payload, warnings = snap.build_snapshot(inventory_path=inv,
                                            adapters_result=built,
                                            real_state_dir=tmp_path,
                                            out_dir=tmp_path)
    payload["harvested_at_utc"] = "2026-08-27T06:00:00Z"
    payload["content_hash"] = "sha256:ctw-travel"
    synth = syn_synthesize(payload)
    claim = synth["clanks"]["chinese-tech-wire"]
    assert claim["state"] in ("HEALTHY", "UNKNOWN")
    assert claim["capability_states"]["collection"]["state"] == "active"
    from motherclank import anomalies as ano
    ledger = ano.detect([payload])
    ctw_gaps = [x for x in ledger if x.get("clank_id") == "chinese-tech-wire"
                and x["type"] == "MATERIALIZATION_GAP"]
    assert not ctw_gaps


def syn_synthesize(payload):  # local helper to avoid import shadowing
    from motherclank import synthesis as s
    return s.synthesize_fleet(payload)
