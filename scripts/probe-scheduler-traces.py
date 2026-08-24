#!/usr/bin/env python3
"""P-4 host-side scheduler-fire trace probe (READ-ONLY).

Emits Motherclank scheduler_traces.py-contract records into
<var>/scheduler/traces.jsonl by querying each lane's REAL scheduler
authority. Never touches a scheduler, never writes a participant DB.

Evidence per class:
  cron            -> `sudo -n journalctl -u cron` positive CRON invocation
                     line naming this lane's deploy_run.sh (evidence_source
                     "journal"). No matching line in the lookback window ->
                     no trace emitted for this run (stays UNKNOWN upstream).
  systemd_system  -> `systemctl show <service>` ExecMainStartTimestamp /
                     ExecMainStatus / Result (evidence_source
                     "timer-lasttrigger"; state is authoritative systemd
                     unit state, not a log/heuristic).
  systemd_user    -> `journalctl --user -u <service>` positive
                     "Finished ..." log line (evidence_source "journal").
  retired/manual  -> no probe run; no trace fabricated.

Run as the identity that owns each unit (anilganti for user-systemd/sudo
cron read, deploy is not required). Idempotent: re-running just appends
freshly-observed traces; nothing is ever deleted or rewritten.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / "motherclank" / "src"))
sys.path.insert(0, str(Path.home() / "diagnostic-clank" / "clank-fleet" / "src"))
from motherclank import scheduler_traces as st  # noqa: E402
from clank_fleet import execution_results as extr  # noqa: E402

HOST_INSTANCE = "ubuntu-4gb-hel1-1"
NOW = datetime.now(UTC).isoformat(timespec="seconds")

# P-4.2: cron-log path per lane, for extractor wiring (read-only tail).
# Only lanes with a registered execution_results extractor are looked up;
# absence of an entry here just means no attestation attempt this pass.
# This is host-specific CONFIG only (a path); the actual block-location
# algorithm is canonical: clank_fleet.execution_results.oem_radar
# .locate_invocation_block (moved there 2026-08-24 during live validation).
CRON_LOG_DIRS = {
    "oem-radar": Path("/home/deploy/staging/oem-radar/logs"),
}


def _oem_output_block(clank_id: str, invoked_at_iso: str) -> str | None:
    """Read-only tail of an append-only log file, handed to the canonical
    locator. Never writes, never touches the participant DB/container."""
    log_dir = CRON_LOG_DIRS.get(clank_id)
    if log_dir is None:
        return None
    try:
        inv_dt = datetime.fromisoformat(invoked_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    log_path = log_dir / f"cron-{inv_dt.strftime('%Y%m%d')}.log"
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if clank_id == "oem-radar":
        from clank_fleet.execution_results.oem_radar import (
            locate_invocation_block,
        )
        return locate_invocation_block(text, invoked_at_iso)
    return None


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.stdout
    except Exception as exc:  # noqa: BLE001 - probe must never crash the caller
        print(f"probe warning: {cmd[0]} failed: {exc}", file=sys.stderr)
        return ""


def cron_trace(clank_id: str, instance_id: str, lane_id: str, match: str) -> dict | None:
    """Positive CRON invocation evidence for one lane's deploy_run.sh."""
    out = _run(["sudo", "-n", "journalctl", "-u", "cron", "--no-pager",
                "-n", "500", "--output=short-iso"])
    best_ts = None
    for line in out.splitlines():
        if match in line and "CMD (" in line:
            m = re.match(r"^(\S+)", line)
            if m:
                best_ts = m.group(1)
    if best_ts is None:
        return None

    attest: dict = {}
    extractor = extr.get_extractor(clank_id)
    if extractor is not None:
        block = _oem_output_block(clank_id, best_ts)
        if block is not None:
            result = extractor.extract(block, exit_code=None)
            if result.get("execution_result") is not None:
                attest["execution_result"] = result["execution_result"]
                attest["execution_detail"] = result.get("execution_detail", "")
                attest["extractor"] = {"id": extractor.id,
                                       "version": extractor.version}

    return st.make_trace(
        trace_id=f"trace-{clank_id}-{best_ts}",
        clank_id=clank_id, instance_id=instance_id, lane_id=lane_id,
        scheduler_type="cron", unit_or_job="deploy-crontab",
        invoked_at=best_ts, process_started=True, exit_or_result=None,
        evidence_source="journal", observed_at=NOW, origin="probe",
        notes=f"positive `journalctl -u cron` CMD line matching '{match}'; "
              "exit/result not observable from cron's own journal entry",
        **attest)


def _systemd_ts_to_iso(value: str) -> str | None:
    """'Mon 2026-08-24 07:03:11 UTC' -> '2026-08-24T07:03:11Z' (UTC only;
    non-UTC systemd timestamps are left unconverted rather than guessed)."""
    if not value or not value.endswith("UTC"):
        return None
    try:
        dt = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def systemd_system_trace(clank_id: str, instance_id: str, lane_id: str,
                         unit: str) -> dict | None:
    out = _run(["systemctl", "show", unit, "-p",
                "ExecMainStartTimestamp,ExecMainExitTimestamp,"
                "ExecMainStatus,Result,ActiveState,InvocationID"])
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    started = _systemd_ts_to_iso(fields.get("ExecMainStartTimestamp", ""))
    if not started:
        return None
    exit_code = fields.get("ExecMainStatus")
    result = fields.get("Result")
    return st.make_trace(
        trace_id=f"trace-{clank_id}-{unit}-{fields.get('InvocationID','')}",
        clank_id=clank_id, instance_id=instance_id, lane_id=lane_id,
        scheduler_type="systemd_system", unit_or_job=unit,
        invoked_at=started,
        process_started=True,
        exit_or_result=(f"exit={exit_code} result={result}"
                        if exit_code is not None else None),
        evidence_source="timer-lasttrigger", observed_at=NOW, origin="probe",
        notes="ExecMainStartTimestamp/ExecMainStatus/Result from "
              "`systemctl show` - authoritative systemd unit state")


def systemd_user_trace(clank_id: str, instance_id: str, lane_id: str,
                       unit: str) -> dict | None:
    out = _run(["journalctl", "--user", "-u", unit, "--no-pager",
                "-n", "50", "--output=short-iso"])
    finished_ts = None
    failed = False
    for line in out.splitlines():
        m = re.match(r"^(\S+).*(Finished|Failed) " + re.escape(unit), line)
        if m:
            finished_ts, verb = m.group(1), m.group(2)
            failed = (verb == "Failed")
    if finished_ts is None:
        return None
    return st.make_trace(
        trace_id=f"trace-{clank_id}-{unit}-{finished_ts}",
        clank_id=clank_id, instance_id=instance_id, lane_id=lane_id,
        scheduler_type="systemd_user", unit_or_job=unit,
        invoked_at=finished_ts, process_started=True,
        exit_or_result="failed" if failed else "ok",
        evidence_source="journal", observed_at=NOW, origin="probe",
        notes=f"positive `journalctl --user -u {unit}` "
              f"{'Failed' if failed else 'Finished'} line")


def main() -> int:
    var_dir = Path.home() / "motherclank" / "var"
    out_path = var_dir / "scheduler" / "traces.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing, warnings = st.load_traces(var_dir)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    existing_ids = {t["trace_id"] for t in existing}

    candidates: list[dict | None] = [
        cron_trace("oem-radar", "oem-radar-hetzner-staging-01", "staging",
                   "staging/oem-radar/deploy/run.sh"),
        cron_trace("smartwatch-clank", "sw-hetzner-staging-01", "staging",
                   "staging/smartwatch-clank/deploy_run.sh"),
        cron_trace("feature-phone-clank", "fpc-hetzner-prod-cron-01", "production",
                   "staging/feature-phone-clank/deploy_run.sh"),
        systemd_system_trace("korean-tech-wire", "korean-tech-wire-hetzner-opt-01",
                             "production", "korean-tech-wire-soak.service"),
        systemd_system_trace("smartphone-clank", "smartphone-clank-hetzner-opt-01",
                             "production",
                             "smartphone-clank-source@google_store_category_phones.service"),
        systemd_user_trace("watch-clank", "watch-clank-hetzner-staging-01",
                           "staging", "watch-clank-watchtime-rss.service"),
        # tablet-clank: RETIRED, no scheduler exists - deliberately no probe call.
    ]

    new = 0
    with out_path.open("a", encoding="utf-8") as f:
        for rec in candidates:
            if rec is None:
                continue
            if rec["trace_id"] in existing_ids:
                continue
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            new += 1

    print(f"probe: {new} new trace(s) appended to {out_path} "
         f"({sum(1 for c in candidates if c is None)} lane(s) with no evidence this run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
