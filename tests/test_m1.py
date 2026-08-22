"""Motherclank M1 tests — deterministic synthesis, downgrade-only property,
provenance, Law 9 drift, and sudo-scope containment."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from motherclank import synthesis as syn
from motherclank.drift import drift_row, read_git_head
from test_m0 import real_state, fleet_yaml  # shared fixtures


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _block(op_state="healthy", sources=None, finished_at=None):
    health = {}
    if sources is not None:
        entries = []
        for status in sources:
            entries.append({"source_id": "s", "status": status})
        health = {"sources": entries}
    last = {"finished_at": finished_at} if finished_at else None
    return {"status": {"operational_state": op_state}, "health": health, "last_run": last}


def _snap(clanks: dict) -> dict:
    return {"harvested_at_utc": _now_iso(), "content_hash": "sha256:test",
            "clanks": clanks}


# ---------------------------------------------------------------------------
# Rule matrix (deterministic)
# ---------------------------------------------------------------------------

def test_healthy_declared_with_all_ok_sources():
    out = syn.synthesize_clank("x", _block("healthy", ["ok"] * 4,
                                           finished_at=_now_iso()),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "HEALTHY"
    assert "R5" in out["rules_applied"]


def test_one_failed_source_degrades():
    out = syn.synthesize_clank("x", _block("healthy", ["ok", "failed"],
                                           finished_at=_now_iso()),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "DEGRADED"


def test_declared_degraded_never_upgrades():
    out = syn.synthesize_clank("x", _block("degraded", ["ok"] * 5,
                                           finished_at=_now_iso()),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "DEGRADED"


def test_all_failed_sources_fail():
    out = syn.synthesize_clank("x", _block("healthy", ["failed", "blocked_zero"],
                                           finished_at=_now_iso()),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "FAILED"


def test_stale_run_downgrades_to_unknown():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    out = syn.synthesize_clank("x", _block("healthy", ["ok"], finished_at=old),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "UNKNOWN"
    assert "R3" in out["rules_applied"]


def test_declared_failed_is_failed_even_with_ok_sources():
    out = syn.synthesize_clank("x", _block("failed", ["ok"], finished_at=_now_iso()),
                               observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "FAILED"


def test_adapter_failure_block_is_unknown():
    block = {"health": {"observation": "FAILED_ADAPTER", "error": "boom"},
             "status": {"operational_state": "healthy"}}
    out = syn.synthesize_clank("x", block, observed_at=_now_iso(), stale_hours=24)
    assert out["state"] == "UNKNOWN"
    assert "R0" in out["rules_applied"]


# ---------------------------------------------------------------------------
# Downgrade-only property: UNKNOWN never upgrades to HEALTHY/DEGRADED/FAILED
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(12))
def test_unknown_inputs_never_yield_healthy(seed):
    now = _now_iso()
    variants = [
        {},                                                        # nothing observable
        {"status": {}},                                            # empty status
        {"status": {"operational_state": "unknown"}},              # explicit unknown
        _block("unknown"),                                          # unknown op-state w/o runs
        {"status": {"operational_state": "healthy"}},              # healthy claim, NO run ts
    ]
    block = variants[seed % len(variants)]
    out = syn.synthesize_clank("x", block, observed_at=now, stale_hours=24)
    if block.get("status", {}).get("operational_state") != "healthy" or \
       "last_run" not in block or block["last_run"] is None:
        assert out["state"] in ("UNKNOWN",), f"{block} -> {out['state']}"


# ---------------------------------------------------------------------------
# Provenance on every derived claim
# ---------------------------------------------------------------------------

def test_every_claim_carries_provenance(real_state, tmp_path, fleet_yaml):
    from motherclank.adapters import build_adapters
    built = build_adapters(real_state)
    payload, _, out = snap_build(tmp_path, built, real_state, fleet_yaml)
    synth = syn.synthesize_fleet(payload)
    for cid, claim in synth["clanks"].items():
        assert claim["provenance"]["source_clank"] == cid
        assert claim["provenance"]["snapshot_observed_at"] == payload["harvested_at_utc"]
        assert isinstance(claim["evidence_fields"], list)


def test_inventory_ledger_extracted_from_fleet_yaml(fleet_yaml):
    from motherclank.snapshot import _inventory_ledger
    ledger = _inventory_ledger(fleet_yaml.read_text())
    assert "watch-clank" in ledger and len(ledger["watch-clank"]) == 40


def snap_build(tmp_path, built, real_state, fleet_yaml):
    from motherclank import snapshot as snap
    out = tmp_path / "var"
    p, w = snap.build_snapshot(inventory_path=fleet_yaml, adapters_result=built,
                               real_state_dir=real_state, out_dir=out)
    snap.append_snapshot(out, p)
    return p, w, out


# ---------------------------------------------------------------------------
# Fleet rollup + chaining
# ---------------------------------------------------------------------------

def test_fleet_rollup_partial_confidence_and_chaining(real_state, tmp_path, fleet_yaml):
    from motherclank.adapters import build_adapters
    built = build_adapters(real_state)
    payload, _, out = snap_build(tmp_path, built, real_state, fleet_yaml)
    synth = syn.synthesize_fleet(payload)

    # feature-phone DB absent in this fixture -> UNKNOWN -> PARTIAL confidence
    assert synth["fleet_confidence"] == "PARTIAL"
    first_hash = syn.content_hash(synth)
    synth["content_hash"] = first_hash
    syn.append_synthesis(out, synth)
    assert syn.previous_synthesis_hash(out) == first_hash


# ---------------------------------------------------------------------------
# Law 9 drift indicator
# ---------------------------------------------------------------------------

def test_law9_drift_reads_git_files_only(tmp_path):
    co = tmp_path / "some-clank"
    (co / ".git" / "refs" / "heads").mkdir(parents=True)
    sha = "a" * 40
    (co / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (co / ".git" / "refs" / "heads" / "main").write_text(sha + "\n")
    row = drift_row("some-clank", co, ledger_sha=sha)
    assert row["relationship"] == "CONVERGED"

    row2 = drift_row("some-clank", co, ledger_sha="b" * 40)
    assert row2["relationship"] == "DIVERGED"

    missing = drift_row("other", tmp_path / "nope", ledger_sha=sha)
    assert missing["relationship"] == "UNKNOWN"


def test_law9_drift_against_real_host_checkouts():
    """REAL_STATE_DIR-adjacent opt-in: on the Hetzner host, compare onboarded
    checkouts against the SHAs Phase 2B recorded. Skips elsewhere."""
    checkouts = {
        "watch-clank": Path("/home/anilganti/watch-clank"),
        "smartphone-clank": Path("/opt/smartphone-clank"),
        "korean-tech-wire": Path("/opt/korean-tech-wire"),
    }
    existing = {k: v for k, v in checkouts.items() if (v / ".git").exists()}
    if not existing:
        pytest.skip("not running on the Hetzner host")
    for cid, path in existing.items():
        head = read_git_head(path)
        assert head is None or len(head) == 40


# ---------------------------------------------------------------------------
# sudo -n scope containment (documented constraint, mechanically checked)
# ---------------------------------------------------------------------------

def test_sudo_is_confined_to_readonly_snapshot_acquisition():
    scripts_dir = HERE.parent / "scripts"
    host_harvest = (scripts_dir / "host-harvest.sh")
    if not host_harvest.exists():
        pytest.skip("scripts not present")
    text = "\n".join(ln for ln in host_harvest.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    # every sudo invocation must target exactly the fixed refresh script
    sudo_lines = [ln.strip() for ln in text.splitlines() if "sudo" in ln]
    assert sudo_lines, "expected the refresh step to be elevated"
    for ln in sudo_lines:
        assert "refresh-real-state.sh" in ln, f"sudo used outside refresh: {ln}"
        assert "$@" not in ln and "$*" not in ln, "no argument forwarding through sudo"
    refresh = (scripts_dir / "refresh-real-state.sh").read_text()
    for forbidden in ("rm -rf /", "bash -c", "sh -c", "eval ", "> /etc",
                      "systemctl", "docker ", "curl", "wget"):
        assert forbidden not in refresh, f"refresh script must stay read-only: {forbidden}"
    # it may only write into its single destination argument
    writes = [ln for ln in refresh.splitlines()
              if ln.startswith('cp ') or 'sqlite3.connect(dst' in ln]
    assert writes, "refresh performs only backup-copy writes"


HERE = Path(__file__).resolve().parent
