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
from motherclank.registry_shim import (  # noqa: E402
    InventoryUnusableError,
    clank_ids_from_inventory,
    operator_registry,
)

FLEET_YAML = """
schema_version: '2.0'
repositories:
- name: watch-clank
  classification: CLANK
  deployment_state: RUNNING
- name: korean-tech-wire
  classification: CLANK
  deployment_state: RUNNING
- name: feature-phone-clank
  classification: CLANK
  deployment_state: RUNNING
- name: diagnostic-clank
  classification: SUPPORTING_SYSTEM
  deployment_state: UNKNOWN
- name: clank-architecture
  classification: GOVERNANCE
  deployment_state: NOT_APPLICABLE
"""


@pytest.fixture
def fleet_yaml(tmp_path):
    """Minimal canonical fleet.yaml mirroring the real schema's semantics."""
    p = tmp_path / "fleet.yaml"
    p.write_text(FLEET_YAML)
    return p


@pytest.fixture
def registry(fleet_yaml):
    return operator_registry(fleet_yaml)


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


def test_cli_bridges_to_inbox_when_given(tmp_path, capsys, fleet_yaml):
    var_dir = tmp_path / "var"
    (var_dir / "anomalies").mkdir(parents=True)
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    (var_dir / "anomalies" / "2026-08-22.jsonl").write_text(json.dumps(batch) + "\n")
    inbox_db = tmp_path / "inbox.db"
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml),
               "--inbox-db", str(inbox_db)])
    assert rc == 0
    assert inbox_db.exists()
    captured = capsys.readouterr()
    assert "delivered=" in captured.out and "producer=motherclank-m3/" in captured.out


# ---------------------------------------------------------------------------
# Remediation tests — merge-review findings B-1/B-2 and identity fail-closed
# ---------------------------------------------------------------------------

# 1-5: inventory-derived registry

def test_inventory_registry_contains_all_valid_clank_ids(fleet_yaml):
    reg = operator_registry(fleet_yaml)
    ids = set(reg.list_ids())
    assert {"watch-clank", "korean-tech-wire", "feature-phone-clank"} <= ids


def test_new_clank_in_inventory_accepted_without_code_change(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(FLEET_YAML + "- name: newly-onboarded-clank\n  classification: CLANK\n")
    reg = operator_registry(p)
    assert "newly-onboarded-clank" in reg.list_ids()


def test_non_clank_repositories_excluded_from_registry(fleet_yaml):
    reg = operator_registry(fleet_yaml)
    ids = set(reg.list_ids())
    assert "diagnostic-clank" not in ids        # SUPPORTING_SYSTEM
    assert "clank-architecture" not in ids      # GOVERNANCE
    # declassification is reflected automatically
    p = fleet_yaml.parent / "demoted.yaml"
    p.write_text(FLEET_YAML.replace(
        "- name: korean-tech-wire\n  classification: CLANK",
        "- name: korean-tech-wire\n  classification: RETIRED"))
    assert "korean-tech-wire" not in set(operator_registry(p).list_ids())


def test_malformed_inventory_fails_loudly(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("repositories: [ { broken\n")
    with pytest.raises(InventoryUnusableError, match="unparseable"):
        clank_ids_from_inventory(bad)
    missing_field = tmp_path / "nofield.yaml"
    missing_field.write_text("schema_version: '2.0'\n")
    with pytest.raises(InventoryUnusableError, match="no usable 'repositories'"):
        clank_ids_from_inventory(missing_field)
    unreadable = tmp_path / "absent.yaml"
    with pytest.raises(InventoryUnusableError, match="unreadable"):
        clank_ids_from_inventory(unreadable)


def test_no_hardcoded_membership_constant_remains():
    import inspect
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "motherclank" / "registry_shim.py"
    text = src.read_text()
    assert "ONBOARDED" not in text, "hard-coded membership list must be gone"
    for leaked in ("watch-clank", "smartphone-clank"):
        assert leaked not in text, f"membership id {leaked} hard-coded in shim"


# 6-11: delivery outcome semantics

def _cli_env(tmp_path, fleet_yaml, monkeypatch=None):
    var_dir = tmp_path / "var"
    (var_dir / "anomalies").mkdir(parents=True)
    batch = _batch([_anom("STALE_RUN_ACTIVE", "watch-clank", "lane-1")])
    (var_dir / "anomalies" / "2026-08-22.jsonl").write_text(json.dumps(batch) + "\n")
    return var_dir


def test_cli_success_reports_delivery_only_after_completion(tmp_path, capsys, fleet_yaml):
    var_dir = _cli_env(tmp_path, fleet_yaml)
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml), "--inbox-db", str(tmp_path / "i.db")])
    captured = capsys.readouterr()
    assert rc == 0
    assert "delivered=" in captured.out          # full-batch success wording
    assert "DELIVERY FAILED" not in captured.err


def test_cli_bridge_failure_exit_7_and_stderr_marker(tmp_path, capsys, fleet_yaml, monkeypatch):
    var_dir = _cli_env(tmp_path, fleet_yaml)
    import motherclank.inbox_bridge as bridge_mod

    def _boom(*a, **kw):
        raise RuntimeError("simulated inbox outage")
    monkeypatch.setattr(bridge_mod, "bridge_recommendations", _boom)
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml), "--inbox-db", str(tmp_path / "i.db")])
    captured = capsys.readouterr()
    assert rc == 7
    assert "inbox: DELIVERY FAILED:" in captured.err
    assert "simulated inbox outage" in captured.err   # cause not swallowed


def test_failed_bridge_never_prints_inbox_success(tmp_path, capsys, fleet_yaml, monkeypatch):
    var_dir = _cli_env(tmp_path, fleet_yaml)
    import motherclank.inbox_bridge as bridge_mod

    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(bridge_mod, "bridge_recommendations", _boom)
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml), "--inbox-db", str(tmp_path / "i.db")])
    captured = capsys.readouterr()
    assert rc == 7
    assert "delivered=" not in captured.out           # no success claim on stdout
    assert "producer=motherclank-m3/" not in captured.out


def test_local_artifacts_intact_when_bridge_fails(tmp_path, capsys, fleet_yaml, monkeypatch):
    var_dir = _cli_env(tmp_path, fleet_yaml)
    import motherclank.inbox_bridge as bridge_mod

    def _boom(*a, **kw):
        raise RuntimeError("late failure after local writes")
    monkeypatch.setattr(bridge_mod, "bridge_recommendations", _boom)
    from motherclank.cli import main
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml), "--inbox-db", str(tmp_path / "i.db")])
    assert rc == 7
    reports = list((tmp_path / "out" / "reports").glob("recommendations-*.md"))
    batches = list((tmp_path / "out" / "recommendations").glob("*.jsonl"))
    assert reports and batches, "local artifacts must survive a bridge failure"
    assert "ADVISORY" in reports[0].read_text()
    captured_err = capsys.readouterr().err
    assert "local recommendation artifacts were written successfully" in captured_err


def test_partial_inbox_failure_not_reported_as_full_success(tmp_path, capsys, fleet_yaml, monkeypatch):
    """Bridge raises midway (after some rows committed) -> exit 7, stderr marker,
    no delivered= success line even though partial rows exist."""
    var_dir = _cli_env(tmp_path, fleet_yaml)
    import motherclank.inbox_bridge as bridge_mod

    def _partial(*a, **kw):
        from clank_runtime.knowledge.inbox import AgentFamily, AgentOutputInbox, OutputType
        # simplest honest simulation: persist one row directly, then explode.
        db_path = kw.get("inbox_db_path")
        inbox = AgentOutputInbox(db_path, kw.get("registry"))
        try:
            inbox.save(agent_family=AgentFamily.MISC, primary_clank_id="watch-clank",
                       raw_text="row one landed then everything broke",
                       output_type=OutputType.RECOMMENDATION,
                       misc_source="motherclank-m3/m3-r1", external_ref="rec-partial")
        finally:
            inbox.close()
        raise RuntimeError("disk full mid-batch")

    monkeypatch.setattr(bridge_mod, "bridge_recommendations", _partial)
    from motherclank.cli import main
    inbox_db = tmp_path / "i.db"
    rc = main(["recommend", "--var-dir", str(var_dir), "--out", str(tmp_path / "out"),
               "--inventory", str(fleet_yaml), "--inbox-db", str(inbox_db)])
    captured = capsys.readouterr()
    assert rc == 7
    assert "inbox: DELIVERY FAILED:" in captured.err
    assert "delivered=" not in captured.out
    assert inbox_db.exists()  # partial rows really did land; still NOT success


# 12-14: identity fail-closed

def test_missing_clank_id_rejected_fail_closed(tmp_path, registry):
    payload = recs.build_batch(tmp_path, _batch([]), [])
    payload["recommendations"] = [{
        "recommendation_id": "rec-orphan", "title": "t", "clank_id": "",
        "subject": "s", "status": "ACTIVE", "priority": "P1", "category": "INVESTIGATION",
        "recommended_action": "a", "cited_anomalies": [], "resolved_citations": [],
        "first_seen": None, "provenance": {}, "generated_from": "",
        "chain_hash": "sha256:x",
    }]
    with pytest.raises(ValueError, match="malformed recommendation identity"):
        bridge_recommendations(payload, inbox_db_path=tmp_path / "x.db",
                               registry=registry, diagnostic_clank_src=_dc_src())


def test_blank_clank_id_never_rewritten_to_fleet_wide(tmp_path, registry):
    payload = recs.build_batch(tmp_path, _batch([]), [])
    payload["recommendations"] = [{
        "recommendation_id": "rec-blank", "title": "t", "clank_id": "   ",
        "subject": "s", "status": "ACTIVE", "priority": "P1", "category": "INVESTIGATION",
        "recommended_action": "a", "cited_anomalies": [], "resolved_citations": [],
        "first_seen": None, "provenance": {}, "generated_from": "",
        "chain_hash": "sha256:x",
    }]
    with pytest.raises(ValueError, match="invalid clank_id"):
        bridge_recommendations(payload, inbox_db_path=tmp_path / "y.db",
                               registry=registry, diagnostic_clank_src=_dc_src())
    # nothing was written misattributed as fleet-wide
    from clank_runtime.knowledge.inbox import AgentOutputInbox
    inbox = AgentOutputInbox(tmp_path / "y.db", registry)
    try:
        assert inbox.list(clank_id="fleet-wide") == []
    finally:
        inbox.close()


def test_no_fleet_wide_type_exists_in_m3_model():
    """The current M3 model has no explicit fleet-wide recommendation type;
    every rule emits an affected clank. The defensive fallback was removed."""
    import inspect
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "motherclank"
           / "inbox_bridge.py").read_text()
    assert '"fleet-wide"' not in src or "never rewritten" in src
    # and the M3 rule table always carries a clank placeholder
    rules_src = (Path(__file__).resolve().parents[1] / "src" / "motherclank"
                 / "recommendations.py").read_text()
    assert "{clank}" in rules_src
