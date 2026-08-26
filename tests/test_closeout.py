"""P-FINAL fleet closeout artifact tests.

The closeout is the anti-archaeology record: per-lane deployment truth,
continuity classification, scheduler attestation, notification capability,
backup evidence and debt — with UNKNOWN preserved wherever an input is
missing. These tests pin the honest-behavior guarantees.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from motherclank.closeout import (  # noqa: E402
    build_closeout,
    validate_live_evidence,
)


def _ev(**over):
    base = {
        "clank_id": "smartwatch-clank",
        "instance_id": "smartwatch-hetzner-cron-lane-01",
        "lane_id": "staging",
        "deployed_commit_sha": "7a5e551bd6cdd313b142ccbdb977a717f2a083a0",
        "environment": "staging",
        "host": "ubuntu-4gb-hel1-1",
        "verified_at_utc": "2026-08-26T11:05:00Z",
        "datastore_identity": "restored-authoritative sqlite (sw-epoch-1-restored)",
        "datastore_path": "/home/deploy/staging/smartwatch-clank/var -> docker volume smartwatch_clank_staging_data",
        "schema_revision": "2",
        "notification_capability": "unsupported_by_policy",
        "source_maturity_summary": "production-allowlist: 4 samsung collectors; no promotion this campaign",
        "backup_recovery_evidence": ["sha256:81967fca pre", "sha256:f67108d5 post"],
        "validation_evidence_ref": "deployment-pass journal 2026-08-26 cycle e764eb93",
        "blocking_debt": [],
        "bounded_debt": ["cli backup summary crash fixed at 29aeeb0; host redeploy pending"],
        "rollback_surface": "images smartwatch-clank:20d5d0d retained",
        "completion_classification": "LIVE_COMPLETE_BOUNDED_DEBT",
    }
    base.update(over)
    return base


def test_closeout_preserves_unknown_everywhere():
    payload = build_closeout(
        generated_at_utc="2026-08-26T12:00:00Z",
        live_evidence=[{
            "clank_id": "some-clank", "instance_id": "UNKNOWN",
            "lane_id": "staging",
        }],
    )
    lane = payload["lanes"][0]
    assert lane["deployment"]["deployed_commit_sha"] == "UNKNOWN"
    assert lane["datastore"]["schema_revision"] == "UNKNOWN"
    assert lane["scheduler"]["attestation"]["SCHEDULER_FIRED"] == "UNKNOWN"
    assert lane["scheduler"]["attestation"]["execution_result"] is None
    assert lane["notification_capability"] == "UNKNOWN"
    assert lane["completion_classification"] == "UNKNOWN"
    assert payload["counts"]["by_completion_classification"] == {"UNKNOWN": 1}


def test_closeout_carries_operator_verbatim_fields_and_hashes():
    ev = _ev()
    payload = build_closeout(generated_at_utc="2026-08-26T12:00:00Z",
                             live_evidence=[ev])
    lane = payload["lanes"][0]
    assert lane["deployment"]["deployed_commit_sha"] == \
        "7a5e551bd6cdd313b142ccbdb977a717f2a083a0"
    assert lane["continuity"]["epoch_id"] == "UNKNOWN"  # no registry given
    assert lane["bounded_debt"] == [
        "cli backup summary crash fixed at 29aeeb0; host redeploy pending"]
    assert lane["latest_live_validation"]["at_utc"] == "2026-08-26T11:05:00Z"
    assert lane["backup_recovery_evidence"][0].startswith("sha256:")
    assert payload["inputs"]["live_evidence_records"] == 1


def test_scheduler_attestation_uses_traces_only():
    trace = {
        "trace_id": "t1", "clank_id": "smartwatch-clank",
        "instance_id": "smartwatch-hetzner-cron-lane-01", "lane_id": "staging",
        "scheduler_type": "cron", "unit_or_job": "deploy_run.sh",
        "invoked_at": "2026-08-26T10:50:02Z", "process_started": True,
        "execution_result": None,  # exit-0 alone never proves an outcome
        "observed_at": "2026-08-26T10:55:00Z",
        "evidence_source": "journal",
    }
    from motherclank.scheduler_traces import make_trace
    trace = make_trace(**trace)
    payload = build_closeout(generated_at_utc="2026-08-26T12:00:00Z",
                             live_evidence=[_ev()],
                             scheduler_traces=[trace])
    att = payload["lanes"][0]["scheduler"]["attestation"]
    assert att["SCHEDULER_FIRED"] == "YES"
    assert att["PROCESS_STARTED"] == "YES"
    # Law: no participant semantic contract in the trace -> result stays null
    assert att["execution_result"] is None


def test_trace_with_proven_participant_semantics_flows_through():
    trace = {
        "trace_id": "t2", "clank_id": "smartphone-clank",
        "instance_id": "x", "lane_id": "soak-staging",
        "scheduler_type": "systemd_system",
        "invoked_at": "2026-08-26T06:50:00Z", "process_started": True,
        "execution_result": "no_work_due",
        "execution_detail":
            "runtime.run_once stdout 'skip samsung_us_owners_product: "
            "not due (interval=180 min)' — due-gating contract traced to "
            "participant code runtime/run_once.py::run_target",
        "extractor": {"id": "motherclank/probe-scheduler-traces.py#journal",
                      "version": 1},
        "observed_at": "2026-08-26T06:56:00Z",
        "evidence_source": "journal",
    }
    from motherclank.scheduler_traces import make_trace
    trace = make_trace(**trace)
    ev = _ev(clank_id="smartphone-clank", instance_id="x",
             lane_id="soak-staging")
    payload = build_closeout(generated_at_utc="2026-08-26T12:00:00Z",
                             live_evidence=[ev],
                             scheduler_traces=[trace])
    att = payload["lanes"][0]["scheduler"]["attestation"]
    assert att["execution_result"] == "no_work_due"
    assert att["trace_id"] == "t2"


def test_validate_live_evidence_rejects_bad_structure():
    errs = validate_live_evidence([{"clank_id": "x"}])
    assert any("instance_id" in e for e in errs)
    errs = validate_live_evidence([
        dict(_ev()),
        dict(_ev()),
    ])
    assert any("duplicate" in e for e in errs)
    bad_cls = _ev(completion_classification="TOTALLY_DONE")
    errs = validate_live_evidence([bad_cls])
    assert any("classification" in e for e in errs)
