"""Motherclank M0 boundary tests.

Proves the four ADR-0002 invariants mechanically:
1. READ-ONLY      — adapter DBs byte-identical across a full harvest; sqlite
                    total_changes == 0; no write-capable SQL in our source.
2. UNKNOWN        — missing tables/lanes propagate as UNKNOWN/null, never 0/healthy.
3. CONTAINMENT    — one broken Clank cannot abort the fleet snapshot.
4. NO MUTATION UI — package source contains no notification/network/mutation imports.

Hermetic except the optional real-state class (REAL_STATE_DIR), mirroring the
Phase 2C validation pattern. Adapter plane is imported UNCHANGED from the
diagnostic-clank sibling checkout, exactly as production does.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from motherclank.adapters import ensure_adapter_plane, build_adapters
from motherclank import snapshot as snap
from motherclank.report import render_report

ensure_adapter_plane()

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
PKG_SRC = HERE.parent / "src" / "motherclank"
FLEET_YAML = WORKSPACE / "diagnostic-clank" / "clank-fleet" / "inventories" / "fleet.yaml"
import os as _os
REAL_STATE_DIR = Path(_os.environ["REAL_STATE_DIR"]) if _os.environ.get("REAL_STATE_DIR") else None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def fleet_yaml(tmp_path: Path) -> Path:
    src = FLEET_YAML if FLEET_YAML.exists() else None
    target = tmp_path / "fleet.yaml"
    if src:
        target.write_text(src.read_text())
    else:
        target.write_text("schema_version: '2.0'\nexpected_repositories: []\n")
    return target


# ---------------------------------------------------------------------------
# Synthetic real-schema DBs (same builders as Phase 2C adapter fixtures)
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_state(tmp_path: Path) -> Path:
    d = tmp_path / "real-state"
    d.mkdir()
    con = sqlite3.connect(d / "watch_clank.db")
    con.executescript(
        """
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE operational_epochs (id INTEGER PRIMARY KEY, name TEXT, started_at TEXT,
            baseline_started_at TEXT, baseline_completed_at TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE collector_runs (id INTEGER PRIMARY KEY, collector_id TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, observation_count INT, is_baseline INT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, status TEXT, created_at TEXT);
        CREATE TABLE event_reviews (id INTEGER PRIMARY KEY, disposition TEXT);
        """
    )
    con.execute("INSERT INTO alembic_version VALUES ('0007_test')")
    con.execute("INSERT INTO collector_runs VALUES (1,'casio_multi','SUCCESS','2026-08-22T05:20:04Z','2026-08-22T05:20:07Z',5,0)")
    con.execute("INSERT INTO events VALUES (1,'DRAFT','2026-08-22T00:23:54Z')")
    con.commit(); con.close()

    con = sqlite3.connect(d / "smartphone_clank.db")
    con.executescript(
        """
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE collector_run_metrics (id INTEGER PRIMARY KEY, collector_name TEXT,
            status TEXT, started_at TEXT, finished_at TEXT, candidates_found INT,
            meaningful_changes INT, alerts_sent INT, run_reason TEXT);
        CREATE TABLE webhook_deliveries (id INTEGER PRIMARY KEY, reason TEXT);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY);
        CREATE TABLE timeline_events (id INTEGER PRIMARY KEY, event_type TEXT);
        """
    )
    con.execute("INSERT INTO alembic_version VALUES ('0007_wave1_baseline_state')")
    con.execute("INSERT INTO collector_run_metrics VALUES (1,'google_store_phones','ok','2026-08-21T20:18:03Z','2026-08-21T20:18:40Z',12,1,1,'production_scheduled')")
    con.execute("INSERT INTO webhook_deliveries VALUES (1,'new_model')")
    con.commit(); con.close()

    con = sqlite3.connect(d / "korean_tech_wire.db")
    con.executescript(
        """
        CREATE TABLE schema_migrations (version INT);
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, finished_at TEXT);
        CREATE TABLE source_run_health (id INTEGER PRIMARY KEY, run_id INT, source_id INT,
            attempted_at TEXT, success INT, references_discovered INT, new_articles INT);
        CREATE TABLE articles (id INTEGER PRIMARY KEY, discovered_at TEXT);
        CREATE TABLE article_feedback (id INTEGER PRIMARY KEY);
        """
    )
    con.execute("INSERT INTO schema_migrations VALUES (4)")
    con.execute("INSERT INTO sources VALUES (1,'sk_hynix_newsroom','PRODUCTION','x')")
    con.execute("INSERT INTO runs VALUES (1,'2026-08-21T22:03:37Z')")
    con.execute("INSERT INTO source_run_health VALUES (1,1,1,'2026-08-10T09:16:00Z',0,5,0)")   # blocked streak
    con.execute("INSERT INTO articles VALUES (1,'2026-08-21T22:03:31Z')")
    con.commit(); con.close()

    # feature-phone: intentionally ABSENT db -> UNKNOWN lane (containment+unknown proof)
    return d


def _harvest(real_state: Path, tmp_path: Path, fleet_yaml: Path):
    built = build_adapters(real_state)
    out = tmp_path / "var"
    payload, warnings = snap.build_snapshot(
        inventory_path=fleet_yaml, adapters_result=built,
        real_state_dir=real_state, out_dir=out)
    return payload, warnings, out


# ---------------------------------------------------------------------------
# 1. read-only behaviour
# ---------------------------------------------------------------------------

def test_harvest_is_byte_level_read_only(real_state, tmp_path, fleet_yaml):
    before = {p.name: _sha(p) for p in sorted(real_state.glob("*.db"))}
    payload, _, out = _harvest(real_state, tmp_path, fleet_yaml)
    after = {p.name: _sha(p) for p in sorted(real_state.glob("*.db"))}
    assert before == after
    assert all(v == 0 for v in payload["read_only_proof_total_changes"].values()), \
        "sqlite total_changes must be zero for every opened Clank DB"


def test_second_harvest_chains_previous_hash(real_state, tmp_path, fleet_yaml):
    built = build_adapters(real_state)
    out = tmp_path / "var"
    first, _ = snap.build_snapshot(inventory_path=fleet_yaml, adapters_result=built,
                                   real_state_dir=real_state, out_dir=out)
    snap.append_snapshot(out, first)
    second, _ = snap.build_snapshot(inventory_path=fleet_yaml, adapters_result=built,
                                    real_state_dir=real_state, out_dir=out)
    snap.append_snapshot(out, second)
    assert second["previous_snapshot_hash"] == first["content_hash"]
    lines = (out / "snapshots").glob("*.jsonl")
    assert sum(1 for f in lines for _ in f.open()) == 2


# ---------------------------------------------------------------------------
# 2. UNKNOWN propagation
# ---------------------------------------------------------------------------

def test_missing_feature_phone_db_is_unknown_not_healthy(real_state, tmp_path, fleet_yaml):
    payload, _, _ = _harvest(real_state, tmp_path, fleet_yaml)
    fp = payload["clanks"]["feature-phone-clank"]
    state = fp["status"].get("operational_state", "")
    assert "UNKNOWN" in str(state).upper()
    report = render_report(payload)
    assert "feature-phone-clank | UNKNOWN" in report


def test_empty_sources_rollup_is_unknown_never_zero(real_state, tmp_path, fleet_yaml):
    """A health block with no recorded entries must roll up as UNKNOWN."""
    built = build_adapters(real_state)
    watch = built["adapters"]["watch-clank"]
    block = watch.health().__dict__
    block["sources"] = []
    rollup = snap.source_rollup(block)
    assert rollup["no_sources_recorded"] is True
    assert rollup["ok"] is None and rollup["unknown"] is None


def test_ktw_blocked_streak_never_reads_healthy(real_state, tmp_path, fleet_yaml):
    payload, _, _ = _harvest(real_state, tmp_path, fleet_yaml)
    ktw = payload["clanks"]["korean-tech-wire"]
    h = ktw["health"]
    entry = [s for s in (h.get("sources") or [])
             if "hynix" in str(s.get("source_id", "") if isinstance(s, dict)
                                else getattr(s, "source_id", ""))
             or "FAILED" in str(s.get("status", getattr(s, "status", ""))).upper()]
    assert entry and all(
        ("failed" in str((s.get("status") if isinstance(s, dict) else getattr(s, "status"))).lower())
        for s in entry), "blocked source must map to FAILED"



# ---------------------------------------------------------------------------
# 3. partial-failure containment
# ---------------------------------------------------------------------------

def test_corrupt_adapter_does_not_abort_fleet(real_state, tmp_path, fleet_yaml):
    (real_state / "smartphone_clank.db").write_bytes(b"this is not a database")
    payload, warnings, _ = _harvest(real_state, tmp_path, fleet_yaml)
    # smartphone isolated as failure...
    sp = payload["clanks"]["smartphone-clank"]
    assert any(
        (isinstance(v, dict) and v.get("observation") == "FAILED_ADAPTER")
        or v == {"observation": "FAILED_ADAPTER"}
        for v in sp.values()
    ), "smartphone failures must be contained inside its own block"
    assert any("smartphone" in w for w in warnings)
    # ...while siblings still observed
    assert payload["clanks"]["watch-clank"]["status"] is not None
    assert "korean-tech-wire" in payload["clanks"]
    # snapshot still hash-chained and complete
    assert payload["content_hash"].startswith("sha256:")


# ---------------------------------------------------------------------------
# 4. absence of mutation-interface imports / statements
# ---------------------------------------------------------------------------

FORBIDDEN = [
    r"\bINSERT\s+INTO", r"\bUPDATE\s+\w+\s+SET", r"\bDELETE\s+FROM",
    r"\bDROP\s+TABLE", r"\bexecutemany\b", r"\bexecutescript\b",
    r"\.commit\s*\(", r"\bimport\s+requests\b", r"\bimport\s+httpx\b",
    r"\bimport\s+socket\b", r"\bimport\s+smtplib\b", r"\bsubprocess\b",
    r"\bos\.system\b", r"\bdiscord\b", r"webhook_url", r"\bpause\b.*\brun_now\b",
]


def test_package_source_has_no_mutation_or_notification_interfaces():
    offenders = []
    for py in PKG_SRC.rglob("*.py"):
        text = py.read_text().lower()
        for pattern in FORBIDDEN:
            if re.search(pattern, text, flags=re.IGNORECASE):
                offenders.append((py.name, pattern))
    assert not offenders, f"forbidden mutation/notification patterns: {offenders}"


def test_cli_dry_run_writes_nothing(real_state, tmp_path, fleet_yaml, capsys):
    out = tmp_path / "dryrun-var"
    code = pytest.importorskip("motherclank.cli").main([
        "harvest", "--inventory", str(fleet_yaml),
        "--real-state", str(real_state),
        "--out", str(out), "--adapters-src",
        str(WORKSPACE / "diagnostic-clank"), "--dry-run",
    ])
    assert code == 0
    assert not out.exists(), "dry-run must not create output directories"


# ---------------------------------------------------------------------------
# 5. real-state validation (opt-in; mirrors Phase 2C pattern)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(REAL_STATE_DIR is None or not Path(REAL_STATE_DIR or ".").exists(),
                    reason="REAL_STATE_DIR not provided")
class TestRealState:
    def test_full_harvest_against_real_host_copies(self, tmp_path, fleet_yaml):
        before = {p.name: _sha(p) for p in sorted(Path(REAL_STATE_DIR).glob("*.db"))}
        payload, warnings, _ = _harvest(Path(REAL_STATE_DIR), tmp_path, fleet_yaml)  # type: ignore[arg-type]
        after = {p.name: _sha(p) for p in sorted(Path(REAL_STATE_DIR).glob("*.db"))}
        assert before == after
        assert all(v == 0 for v in payload["read_only_proof_total_changes"].values())
        assert set(payload["clanks"]) >= {
            "watch-clank", "smartphone-clank", "korean-tech-wire", "feature-phone-clank"}
        # real KTW blocked specimen stays non-healthy
        ktw_health = payload["clanks"]["korean-tech-wire"]["health"]
        sources = ktw_health.get("sources") or []
        sk = [s for s in sources
              if "hynix" in str(s.get("source_id", "") if isinstance(s, dict)
                                 else getattr(s, "source_id", "")).lower()]
        assert sk, "SK hynix must be present in real KTW state"
        status_val = (sk[0].get("status") if isinstance(sk[0], dict)
                      else getattr(sk[0], "status", ""))
        assert "ok" != str(status_val).split(".")[-1].lower(), \
            "real blocked source must not read healthy-by-history"
