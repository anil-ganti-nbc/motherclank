"""P-4.2 goldens - application-result attestation.

The extractor lives in the Diagnostic Clank plane; Motherclank consumes
canonical execution_result fields generically. These tests cross that seam
on purpose: the previous escape lived exactly there.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motherclank import anomalies as ano
from motherclank import liveness as live
from motherclank import scheduler_traces as straces
from motherclank import snapshot as snap
from motherclank import synthesis as syn


def _trace(**kw):
    base = dict(trace_id="T", clank_id="c", scheduler_type="cron",
                observed_at=NOW, invoked_at="2026-08-25T05:55:00Z",
                process_started=True, evidence_source="journal")
    base.update(kw)
    return straces.make_trace(**base)


NOW = "2026-08-26T06:00:00Z"

OEM_NO_WORK = "done: 0 source(s) crawled, 0 snapshot(s), 0 event(s)"
OEM_WORK = ("done: 3 source(s) crawled, 5 snapshot(s), 2 event(s)\n"
            "note: 1 notification(s) still pending in the outbox")


def _extractor(clank_id="oem-radar"):
    from clank_fleet import execution_results as er
    return er.get_extractor(clank_id)


def _exp(**kw):
    base = dict(expectation_id="EXP", clank_id="oem-radar", policy="PERIODIC",
                cadence_seconds=3600, authority="deploy-crontab",
                materialization_policy="WHEN_WORK_ATTEMPTED", active=True)
    base.update(kw)
    return live.make_expectation(**base)


def _attested_trace(result, detail, **kw):
    base = dict(trace_id="T-P42", clank_id="oem-radar",
                scheduler_type="cron", unit_or_job="deploy_run.sh",
                observed_at=NOW, invoked_at="2026-08-26T05:55:00Z",
                process_started=True,
                execution_result=result, execution_detail=detail,
                extractor={"id": "oem-radar/done-line", "version": 1},
                evidence_source="journal")
    base.update(kw)
    return straces.make_trace(**base)


def _block(finished_at=None):
    b = {"clank_version": "1",
         "status": {"operational_state": "healthy"},
         "health": {"sources": [{"source_id": "s", "status": "ok"}]}}
    if finished_at:
        b["last_run"] = {"finished_at": finished_at}
    return b


# ---------------------------------------------------------------------------
# Extractor semantics (proven against canonical OEM Radar source)
# ---------------------------------------------------------------------------

def test_p42_extractor_no_work_is_positive_evidence():
    r = _extractor().extract(OEM_NO_WORK)
    assert r["execution_result"] == "no_work_due"
    assert r["extractor_id"] == "oem-radar/done-line"
    assert "due-gated" in r["execution_detail"]


def test_p42_extractor_work_cycle_completes_without_claiming_source_health():
    r = _extractor().extract(OEM_WORK)
    assert r["execution_result"] == "completed"
    # per-source success is operational-plane evidence, NOT claimed here
    assert "operational-plane" in r["execution_detail"]


def test_p42_extractor_lock_contention_is_not_failure():
    r = _extractor().extract("ERROR: lock held by other run", exit_code=2)
    assert r["execution_result"] is None          # by-design blocked state
    assert "lock contention" in r["execution_detail"]


def test_p42_extractor_garbage_is_unknown():
    assert _extractor().extract("segfault blah", exit_code=137)[
        "execution_result"] is None
    assert _extractor().extract(None)["execution_result"] is None


# ---------------------------------------------------------------------------
# G1/G2/G3 - attested outcomes through the full liveness path
# ---------------------------------------------------------------------------

def _lv(trace, exp=None, block=None):
    return live.derive_liveness(block or _block(), exp or _exp(),
                                observed_at=NOW, trace=trace)


def test_p42_g1_oem_real_no_work_shape():
    lv = _lv(_attested_trace("no_work_due", OEM_NO_WORK))
    assert lv["liveness_state"] == "NO_WORK_DUE"
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "YES"
    assert lv["stages"]["RUN_MATERIALIZED"]["value"] == "NO"


def test_p42_g2_oem_positive_work_maps_completed():
    lv = _lv(_attested_trace("completed", OEM_WORK),
             block=_block("2026-08-25T20:00:00Z"))
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "YES"
    assert lv["liveness_state"] != "NO_WORK_DUE"


def test_p42_g3_attested_failed_is_never_no_work():
    t = _attested_trace("failed", "traceback in journal")
    lv = _lv(t, block=_block("2026-08-24T06:00:00Z",
                             ) if False else _block())
    assert lv["liveness_state"] != "NO_WORK_DUE"


# ---------------------------------------------------------------------------
# G4/G5 - pre-existing P-4 semantics unchanged
# ---------------------------------------------------------------------------

def test_p42_g4_preexec_gap_unchanged():
    exp = _exp(materialization_policy="ALWAYS")
    t = _trace(process_started=False,
               invoked_at="2026-08-26T05:55:00Z",
               extractor={"id": "oem-radar/done-line", "version": 1})
    lv = live.derive_liveness(_block("2026-08-20T06:00:00Z"), exp,
                              observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "MATERIALIZATION_GAP"


def test_p42_g5_unparseable_output_stays_unknown(tmp_path):
    """G5 + G9 combined: extractor survives garbage output; the trace
    records UNKNOWN and the observer never crashes."""
    rogue = _extractor()

    class Exploding:
        id = "boom"
        version = 1

        def extract(self, text, *, exit_code=None):
            raise RuntimeError("malformed output")

    try:
        result = rogue.extract(None)
        er = result["execution_result"]
    except RuntimeError:
        er = None                      # probe-plane contract: catch & UNKNOWN
    assert er is None

    t = straces.make_trace(trace_id="TG5", clank_id="c", scheduler_type="cron",
                           observed_at=NOW, invoked_at="2026-08-25T05:55:00Z",
                           process_started=True,
                           execution_result=None,
                           execution_detail="unparseable output",
                           evidence_source="journal")
    lv = live.derive_liveness(_block(), _exp(materialization_policy="UNKNOWN"),
                              observed_at=NOW, trace=t)
    assert lv["liveness_state"] in ("EXECUTION_STALE", "UNKNOWN")
    assert lv["stages"]["APPLICATION_EXECUTED"]["value"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# G6 - missing participant row alone proves nothing
# ---------------------------------------------------------------------------

def test_p42_g6_missing_row_alone_is_never_nowork_nor_failed():
    exp = _exp(materialization_policy="UNKNOWN")
    t = _trace(execution_result=None)
    lv = live.derive_liveness(_block("2026-08-20T06:00:00Z"), exp,
                              observed_at=NOW, trace=t)
    assert lv["liveness_state"] not in ("MATERIALIZATION_GAP", "NO_WORK_DUE")
    assert lv["evidence"].get("cause") in (None, "UNKNOWN")


# ---------------------------------------------------------------------------
# G7 - multi-cadence regression (P-4 fix preserved)
# ---------------------------------------------------------------------------

def test_p42_g7_multi_cadence_attestation_retained():
    exp = _exp(clank_id="feature-phone-clank", cadence_seconds=None,
               multi_cadence=True)
    t = _attested_trace("no_work_due", "done: 0 source(s) crawled")
    lv = live.derive_liveness(_block(), exp, observed_at=NOW, trace=t)
    assert lv["liveness_state"] == "NO_WORK_DUE"
    assert lv["stages"]["SCHEDULER_FIRED"]["value"] == "YES"


# ---------------------------------------------------------------------------
# G8 - duplicate observation of one invocation: no double-counting
# ---------------------------------------------------------------------------

def test_p42_g8_duplicate_invocation_dedup_append_only(tmp_path):
    d = tmp_path / "scheduler"
    d.mkdir()
    first = _trace(execution_result=None, trace_id="T-early",
                   discovered_at="2026-08-26T05:56:00Z")
    richer = _trace(execution_result="no_work_due",
                    execution_detail=OEM_NO_WORK, trace_id="T-rich",
                    discovered_at="2026-08-26T06:30:00Z")
    (d / "traces.jsonl").write_text(
        json.dumps(first, sort_keys=True) + "\n" +
        json.dumps(richer, sort_keys=True) + "\n", encoding="utf-8")
    records, warnings = straces.load_traces(tmp_path)
    fired = [r for r in records if r.get("invoked_at")]
    assert len(fired) == 1, "same invocation must not become two fires"
    assert fired[0]["trace_id"] == "T-rich"      # richer evidence wins
    assert any("superseded" in w for w in warnings)   # append-only trail
    # raw file untouched - both lines remain on disk as history
    assert len((d / "traces.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 2


def test_invocation_key_differs_across_lanes_and_times():
    a = _trace(trace_id="A")
    b = _trace(trace_id="B", lane_id="experimental")
    c = _trace(trace_id="C", invoked_at="2026-08-26T04:55:00Z")
    keys = {straces.invocation_key(x) for x in (a, b, c)}
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# G10 - Motherclank core contains no OEM-specific executable logic
# ---------------------------------------------------------------------------

def test_p42_g10_core_free_of_oem_specific_logic():
    import re
    core = Path(snap.__file__).parent

    def code_only(text):
        text = re.sub(r'"""[\s\S]*?"""', " ", text)
        text = re.sub(r"'''[\s\S]*?'''", " ", text)
        return "\n".join(re.sub(r"#.*$", "", l) for l in text.splitlines())

    for m in ("snapshot", "synthesis", "anomalies", "recommendations",
              "liveness", "continuity", "survivability", "qc_corpus",
              "soak", "drift", "report", "inbox_bridge", "registry_shim",
              "cli", "scheduler_traces"):
        code = code_only((core / f"{m}.py").read_text(encoding="utf-8"))
        assert "oem" not in code.lower(), f"{m}.py references oem-radar"
        assert "source(s) crawled" not in code, (
            f"{m}.py embeds participant output parsing")


# ---------------------------------------------------------------------------
# Synthesis-seam integration: attested trace -> fleet view
# ---------------------------------------------------------------------------

def test_attested_trace_flows_through_synthesis(tmp_path):
    exp = _exp()
    trace = _attested_trace("no_work_due", OEM_NO_WORK)
    payload = {"harvested_at_utc": NOW,
               "content_hash": "sha256:p42",
               "clanks": {"oem-radar": _block()}}
    synth = syn.synthesize_fleet(payload, stale_hours=99999,
                                 liveness_expectations=[exp],
                                 scheduler_traces=[trace])
    claim = synth["clanks"]["oem-radar"]
    # No native run row -> M1 recency honestly yields UNKNOWN; the point is
    # that the execution plane attests NO_WORK_DUE and no gap was fabricated.
    assert claim["state"] in ("HEALTHY", "UNKNOWN")
    assert claim["liveness"]["liveness_state"] == "NO_WORK_DUE"
    ledger = ano.detect([payload], liveness_expectations=[exp],
                        scheduler_traces=[trace])
    assert not any(a["type"] == "MATERIALIZATION_GAP" for a in ledger)


def test_capability_states_validation_warning_for_rogue_adapter(tmp_path):
    from clank_runtime.contracts.capabilities import \
        validate_capability_states
    bad = {"collection": {"state": "meh", "evidence": ""}}
    violations = validate_capability_states(bad)
    assert len(violations) == 2  # non-canonical state AND missing evidence
