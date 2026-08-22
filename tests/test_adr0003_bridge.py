"""ADR-0003 bridge tests: Motherclank M3 recommendations -> Agent Inbox.

Covers the operator-mandated test list: round-trip, external_ref ==
recommendation_id, MISC provenance, content dedup, version series under one
logical recommendation, identity stability across RULES_VERSION bumps, local
Markdown generation unchanged, dry-run zero writes, and the existing
deterministic/boundary suites staying green.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motherclank.adapters import ensure_adapter_plane
from motherclank import recommendations as recs
from motherclank.inbox_bridge import bridge_recommendations, render_recommendation_text

# clank_runtime resolves via the adapter-plane sys.path bootstrap; it must run
# BEFORE importing registry_shim, which imports clank_runtime at module level.
ensure_adapter_plane()
from motherclank.registry_shim import operator_registry  # noqa: E402


def _anom(atype, clank, subject="s1", lifecycle="ONGOING", severity="HIGH"):
    return {
        "anomaly_id": f"aid-{atype}-{clank}-{subject}",
        "type": atype, "severity": severity, "clank_id": clank,
        "subject": subject,
        "first_seen": "2026-08-22T06:40:03+00:00",
        "last_seen": "2026-08-22T07:39:55+00:00",
        "lifecycle": lifecycle,
        "evidence": [{"observed_at": "2026-08-22T07:39:55+00:00",
                      "detail": f"{atype} observed"}],
    }


def _batch(anomalies, generated_from="2026-08-22T07:39:55+00:00",
           batch_hash="sha256:input"):
    return {"batch_generated_from": generated_from,
            "batch_hash": batch_hash, "anomalies": anomalies}


@pytest.fixture
def registry():
    return operator_registry()


# -- round-trip / provenance ---------------------------------------------------

def test_bridge_roundtrip_external_ref_and_provenance(tmp_path, registry):
    batch = _batch([_anom("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia")])
    recs_list = recs.derive_recommendations(batch)
    payload = recs.build_batch(tmp_path, batch, recs_list)
    db = tmp_path / "inbox.db"
    summary = bridge_recommendations(payload, inbox_db_path=db, registry=registry,
                                     diagnostic_clank_src=_dc_src(),
                                     rules_version=payload["rules_version"])
    from clank_runtime.knowledge.inbox import AgentFamily, AgentOutputInbox, OutputType
    inbox = AgentOutputInbox(db, registry)
    try:
        assert len(summary["saved"]) == len(recs_list) > 0
        for entry in summary["saved"]:
            out = inbox.get(entry["output_id"])
            assert out.output_type == OutputType.RECOMMENDATION
            assert out.agent_family == AgentFamily.MISC          # no MOTHERCLANK family
            assert out.misc_source == f"motherclank-m3/{payload['rules_version']}"
            assert out.external_ref == entry["recommendation_id"]
            assert out.external_ref.startswith("rec-")
    finally:
        inbox.close()


def _dc_src():
    """Locate the diagnostic-clank checkout for the adapter-plane path helper."""
    here = Path(__file__).resolve()
    for base in (*here.parents,):
        cand = base / "diagnostic-clank"
        if cand.exists():
            return cand
    return None


# -- dedup / version series ----------------------------------------------------

def test_identical_emission_dedups(tmp_path, registry):
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    payload = recs.build_batch(tmp_path, batch, recs.derive_recommendations(batch))
    db = tmp_path / "inbox.db"
    s1 = bridge_recommendations(payload, inbox_db_path=db, registry=registry,
                                diagnostic_clank_src=_dc_src())
    s2 = bridge_recommendations(payload, inbox_db_path=db, registry=registry,
                                diagnostic_clank_src=_dc_src())
    assert len(s1["saved"]) == len(s2["saved"])
    assert s2["deduplicated"] == len(s1["saved"])  # second pass fully deduped


def test_changed_citations_same_logical_recommendation_new_row(tmp_path, registry):
    batch1 = _batch([_anom("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia")],
                    batch_hash="sha256:one")
    # same logical recommendation, evolved evidence (new detail + batch hash)
    a2 = _anom("PERSISTENT_BLOCKED_STREAK", "feature-phone-clank", "hmd-nokia")
    a2["evidence"] = [{"observed_at": "2026-08-22T08:39:55+00:00",
                       "detail": "PERSISTENT_BLOCKED_STREAK still blocked, 5th cycle"}]
    batch2 = _batch([a2], batch_hash="sha256:two")
    db = tmp_path / "inbox.db"
    s1 = bridge_recommendations(recs.build_batch(tmp_path, batch1, recs.derive_recommendations(batch1)),
                                inbox_db_path=db, registry=registry, diagnostic_clank_src=_dc_src())
    s2 = bridge_recommendations(recs.build_batch(tmp_path, batch2, recs.derive_recommendations(batch2)),
                                inbox_db_path=db, registry=registry, diagnostic_clank_src=_dc_src())
    rid = s1["saved"][0]["recommendation_id"]
    assert s2["saved"][0]["recommendation_id"] == rid   # same logical identity
    assert s2["deduplicated"] == 0                       # changed content -> new row, not dedup
    from clank_runtime.knowledge.inbox import AgentOutputInbox
    inbox = AgentOutputInbox(db, registry)
    try:
        latest = inbox.latest_by_external_ref(rid)
        assert latest is not None and "5th cycle" in latest.raw_text
        # earlier version remains intact
        all_versions = [inbox.get(e["output_id"]) for e in s1["saved"] + s2["saved"]]
        assert len({r.output_id for r in all_versions}) == 2
    finally:
        inbox.close()


def test_severity_lifecycle_changes_preserve_external_ref(tmp_path, registry):
    low = _anom("SOURCE_HEALTH_TRANSITION", "korean-tech-wire", "etnews", severity="MEDIUM")
    high = _anom("SOURCE_HEALTH_TRANSITION", "korean-tech-wire", "etnews", severity="HIGH")
    db = tmp_path / "inbox.db"
    s1 = bridge_recommendations(recs.build_batch(tmp_path, _batch([low]), recs.derive_recommendations(_batch([low]))),
                                inbox_db_path=db, registry=registry, diagnostic_clank_src=_dc_src())
    s2 = bridge_recommendations(recs.build_batch(tmp_path, _batch([high]), recs.derive_recommendations(_batch([high]))),
                                inbox_db_path=db, registry=registry, diagnostic_clank_src=_dc_src())
    assert s1["saved"][0]["recommendation_id"] == s2["saved"][0]["recommendation_id"]


# -- identity stability across RULES_VERSION bump ------------------------------

def test_recommendation_id_stable_across_rules_version_bump(tmp_path, monkeypatch):
    """rule_key/clank_id/subject_group unchanged -> same recommendation_id,
    regardless of RULES_VERSION. Enforced by ADR-0003 §2.3."""
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    rid_v1 = recs.derive_recommendations(batch)[0]["recommendation_id"]
    monkeypatch.setattr(recs, "RULES_VERSION", "m3-r2")
    rid_v2 = recs.derive_recommendations(batch)[0]["recommendation_id"]
    assert rid_v1 == rid_v2


def test_render_is_deterministic(tmp_path):
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    rec = recs.derive_recommendations(batch)[0]
    assert render_recommendation_text(rec, batch) == render_recommendation_text(rec, batch)


# -- CLI integration: local Markdown unchanged + dry-run zero writes -----------

def test_cli_recommend_local_markdown_still_works(tmp_path, monkeypatch, capsys):
    var_dir = tmp_path / "var"
    (var_dir / "anomalies").mkdir(parents=True)
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    (var_dir / "anomalies" / "2026-08-22.jsonl").write_text(
        json.dumps(batch) + "\n")
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out")])
    assert rc == 0
    reports = list((tmp_path / "out" / "reports").glob("recommendations-*.md"))
    assert reports and "ADVISORY" in reports[0].read_text()
    assert not (tmp_path / "inbox.db").exists()  # no bridge without --inbox-db


def test_cli_dry_run_zero_writes(tmp_path, monkeypatch, capsys):
    var_dir = tmp_path / "var"
    (var_dir / "anomalies").mkdir(parents=True)
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    (var_dir / "anomalies" / "2026-08-22.jsonl").write_text(json.dumps(batch) + "\n")
    inbox_db = tmp_path / "inbox.db"
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inbox-db", str(inbox_db), "--dry-run"])
    assert rc == 0
    assert not inbox_db.exists(), "dry-run must not create the Inbox DB"
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*")), \
        "dry-run must not write var/ reports or batches"


def test_cli_bridges_to_inbox_when_given(tmp_path, capsys):
    var_dir = tmp_path / "var"
    (var_dir / "anomalies").mkdir(parents=True)
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    (var_dir / "anomalies" / "2026-08-22.jsonl").write_text(json.dumps(batch) + "\n")
    inbox_db = tmp_path / "inbox.db"
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inbox-db", str(inbox_db)])
    assert rc == 0
    assert inbox_db.exists()
    out = capsys.readouterr().out
    assert "inbox:" in out and "producer=motherclank-m3/" in out
