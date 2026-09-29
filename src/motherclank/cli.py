"""Motherclank M0 CLI.

    motherclank harvest --inventory fleet.yaml --real-state DIR [--out DIR] [--dry-run]

Read-only by construction: the only files written are Motherclank's own
snapshot/report outputs. --dry-run prints the report and the snapshot payload
without touching disk beyond stdout.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import sys
from pathlib import Path

from .adapters import AdapterPlaneUnavailable, build_adapters
from . import snapshot as snap
from . import snapshot_manifest as manifest_v1
from .snapshot_manifest import SnapshotManifestError
from . import synthesis as syn
from . import continuity as cont
from . import liveness as live
from . import scheduler_traces as straces
from .drift import drift_row, DEFAULT_HETZNER_CHECKOUTS
from .report import render_report, write_report, render_synthesis, render_anomalies, render_recommendations
from . import anomalies as ano
from . import recommendations as recs
from . import qc_corpus as qc
from . import soak


def main(argv: list[str] | None = None) -> int:
    # F4: bridge/report output contains non-ASCII markers; never let a
    # platform legacy codec (e.g. cp1252) turn reporting into a crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="motherclank")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest", help="read-only fleet observation snapshot")
    h.add_argument("--inventory", required=True, type=Path, help="path to fleet.yaml")
    h.add_argument("--real-state", required=True, type=Path,
                   help="directory holding read-only DB copies (watch_clank.db, ...)")
    h.add_argument("--adapters-src", type=Path, default=None,
                    help="path to diagnostic-clank checkout if not a workspace sibling")
    h.add_argument("--adapter-registry", type=Path, default=None,
                    help="optional adapter registry file (JSON/YAML); extends builtin set")
    h.add_argument("--snapshot-manifest", type=Path, default=None,
                   help="ADR-0016 v1.0 per-lane provenance manifest; required for NAS candidate proof")
    h.add_argument("--expected-adapter-package-sha", type=str, default=None,
                   help="launcher-verified Diagnostic adapter source Git SHA; required with v1 manifest")
    h.add_argument("--expected-adapter-artifact-sha256", type=str, default=None,
                   help="launcher-verified adapter-bearing image/package digest; required with v1 manifest")
    h.add_argument("--out", type=Path, default=Path("var"), help="output directory")
    h.add_argument("--dry-run", action="store_true",
                   help="compute and print; write nothing")
    z = sub.add_parser("synthesize", help="derive fleet health from the latest M0 snapshot")
    z.add_argument("--var-dir", required=True, type=Path,
                   help="M0 output directory containing snapshots/")
    z.add_argument("--out", type=Path, default=Path("var"))
    z.add_argument("--stale-hours", type=float, default=24.0)
    z.add_argument("--drift-checkouts", type=Path, default=None,
                   help="optional JSON {clank: checkout_path} for Law 9 metric")
    z.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("detect", help="deterministic anomaly ledger from M0/M1 history")
    d.add_argument("--var-dir", required=True, type=Path)
    d.add_argument("--out", type=Path, default=Path("var"))
    d.add_argument("--dry-run", action="store_true")
    rr = sub.add_parser("recommend", help="advisory operator recommendations from the anomaly ledger")
    rr.add_argument("--var-dir", required=True, type=Path)
    rr.add_argument("--out", type=Path, default=Path("var"))
    rr.add_argument("--dry-run", action="store_true")
    rr.add_argument("--inbox-db", type=Path, default=None,
                    help="ADR-0003 bridge: Agent Inbox SQLite path; omit to skip bridging")
    rr.add_argument("--inventory", type=Path, default=None,
                    help="canonical fleet.yaml; REQUIRED with --inbox-db (registry seed source)")
    rr.add_argument("--adapters-src", type=Path, default=None,
                    help="diagnostic-clank checkout root (defaults to workspace sibling)")
    q = sub.add_parser("ingest-qc", help="append-only human-QC corpus from read-only adapters")
    q.add_argument("--real-state", required=True, type=Path)
    q.add_argument("--var-dir", required=True, type=Path)
    q.add_argument("--out", type=Path, default=Path("var"))
    q.add_argument("--adapters-src", type=Path, default=None)
    q.add_argument("--adapter-registry", type=Path, default=None)
    q.add_argument("--inventory", type=Path, default=None,
                   help="same fleet inventory used by governed M0 harvest")
    q.add_argument("--snapshot-manifest", type=Path, default=None,
                   help="ADR-0016 v1.0 manifest used by latest governed M0 harvest")
    q.add_argument("--expected-adapter-package-sha", type=str, default=None,
                   help="launcher-verified adapter source SHA for governed QC")
    q.add_argument("--expected-adapter-artifact-sha256", type=str, default=None,
                   help="launcher-verified adapter artifact digest for governed QC")
    q.add_argument("--dry-run", action="store_true")
    sr = sub.add_parser("soak-report", help="periodic QC-soak report + M5 gate scoring (Axis B)")
    sr.add_argument("--var-dir", required=True, type=Path)
    sr.add_argument("--out", type=Path, default=Path("var"))
    sr.add_argument("--as-of", type=str, default=None,
                    help="ISO timestamp anchor; defaults to latest batch")
    sr.add_argument("--dry-run", action="store_true")
    cv = sub.add_parser("validate-continuity",
                        help="read-only validation of the append-only continuity registry")
    cv.add_argument("--var-dir", required=True, type=Path)
    co = sub.add_parser("closeout",
                        help="assemble the canonical machine-readable fleet closeout record")
    co.add_argument("--live-evidence", required=True, type=Path,
                    help="operator-verified JSONL evidence entries (structural, hashed)")
    co.add_argument("--lane-configs", type=Path)
    co.add_argument("--continuity-events", type=Path,
                    help="JSONL continuity events (validated; invalid lines skipped with warning)")
    co.add_argument("--scheduler-traces", type=Path,
                    help="P-4 traces.jsonl (attestation stays UNKNOWN without them)")
    co.add_argument("--out", type=Path,
                    help="write the closeout payload here (stdout always)")
    args = parser.parse_args(argv)

    if args.command == "harvest":
        return _harvest(args)
    if args.command == "synthesize":
        return _synthesize(args)
    if args.command == "detect":
        return _detect(args)
    if args.command == "recommend":
        return _recommend(args)
    if args.command == "ingest-qc":
        return _ingest_qc(args)
    if args.command == "soak-report":
        return _soak_report(args)
    if args.command == "validate-continuity":
        events, warnings = cont.load_events(args.var_dir)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"continuity registry: {len(events)} valid event(s), "
              f"registry_hash={cont.registry_hash(events) if events else 'empty'}")
        return 1 if warnings else 0
    if args.command == "closeout":
        from .closeout import build_closeout_from_files

        try:
            payload, warnings = build_closeout_from_files(
                generated_at_utc=datetime.now(UTC).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
                live_evidence_path=args.live_evidence,
                lane_configs_path=args.lane_configs,
                continuity_events_path=args.continuity_events,
                scheduler_traces_path=args.scheduler_traces,
            )
        except ValueError as exc:
            print(f"closeout refused: {exc}", file=sys.stderr)
            return 2
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 1 if warnings else 0
    return 2


def _load_continuity(var_dir: Path):
    """Load the F6 continuity registry if present; surface warnings honestly."""
    events, warnings = cont.load_events(var_dir)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return events


def _load_liveness(var_dir: Path):
    """Load the F6b execution-expectations registry if present."""
    expectations, warnings = live.load_expectations(var_dir)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return expectations


def _load_scheduler_traces(var_dir: Path):
    """Load P-4 scheduler-fire traces if the probe plane has provided any."""
    traces, warnings = straces.load_traces(var_dir)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return traces


def _governed_qc_gate(args, built: dict, latest: dict | None) -> dict[str, dict]:
    """Attest each QC input against the latest M0 and the copy *now*.

    No QC adapter's qc_records() is called until this gate succeeds. The
    latest M0 proves which governed attempt the corpus cites; a new read-only
    harvest of that same manifest verifies copy, clock, schema, runtime and
    observer contract again at QC time.
    """
    if not isinstance(latest, dict) or latest.get("snapshot_contract_version") != "1.0":
        raise SnapshotManifestError("governed QC needs a latest snapshot-v1 M0 harvest")
    if args.snapshot_manifest is None or args.inventory is None:
        raise SnapshotManifestError(
            "governed QC requires --snapshot-manifest and --inventory")
    if not args.inventory.is_file():
        raise SnapshotManifestError("governed QC inventory missing")
    saved_hash = latest.get("content_hash")
    if not isinstance(saved_hash, str) or saved_hash != snap.content_hash({
            key: value for key, value in latest.items() if key != "content_hash"}):
        raise SnapshotManifestError("latest M0 snapshot content hash invalid")

    current, _ = snap.build_snapshot(
        inventory_path=args.inventory,
        adapters_result=built,
        real_state_dir=args.real_state,
        out_dir=args.var_dir,
        snapshot_manifest_path=args.snapshot_manifest,
        expected_adapter_package_sha=args.expected_adapter_package_sha,
        expected_adapter_artifact_sha256=args.expected_adapter_artifact_sha256,
    )
    if current.get("snapshot_manifest_sha256") != latest.get(
            "snapshot_manifest_sha256"):
        raise SnapshotManifestError("governed QC manifest differs from latest M0")
    if current.get("inventory_revision") != latest.get("inventory_revision"):
        raise SnapshotManifestError("governed QC inventory differs from latest M0")
    if current.get("adapter_contract_versions") != latest.get(
            "adapter_contract_versions"):
        raise SnapshotManifestError("governed QC adapter contracts differ from latest M0")
    latest_registry = latest.get("adapter_registry_entries")
    if not isinstance(latest_registry, dict):
        raise SnapshotManifestError("governed QC latest M0 registry missing")
    intended_qc = {
        cid for cid, entry in latest_registry.items()
        if isinstance(entry, dict) and entry.get("qc") is True}
    if intended_qc != set(built["qc_adapters"]):
        raise SnapshotManifestError("governed QC enabled lane set differs from latest M0")
    for field in ("adapter_registry_source_sha256",
                  "adapter_registry_effective_sha256",
                  "adapter_registry_entries"):
        if current.get(field) != latest.get(field):
            raise SnapshotManifestError(
                f"governed QC {field} differs from latest M0")
    if (not isinstance(latest.get("inventory_sha256"), str)
            or current.get("inventory_sha256") != latest["inventory_sha256"]):
        raise SnapshotManifestError("governed QC inventory bytes differ from latest M0")
    manifest = manifest_v1.load_manifest(args.snapshot_manifest)
    if manifest["_verified_manifest_sha256"] != current["snapshot_manifest_sha256"]:
        raise SnapshotManifestError("governed QC manifest changed during gate")
    producer_lanes = {row["clank_id"]: row for row in manifest["lanes"]}

    source_provenance: dict[str, dict] = {}
    for cid in built["qc_adapters"]:
        old_block = (latest.get("clanks") or {}).get(cid)
        new_block = (current.get("clanks") or {}).get(cid)
        if not isinstance(old_block, dict) or not isinstance(new_block, dict):
            raise SnapshotManifestError(f"{cid}: QC lane absent from governed M0")
        old = old_block.get("snapshot_provenance")
        new = new_block.get("snapshot_provenance")
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise SnapshotManifestError(f"{cid}: QC snapshot provenance missing")
        if (old.get("effective_freshness_state") != "FRESH"
                or new.get("effective_freshness_state") != "FRESH"
                or old_block.get("observation") or new_block.get("observation")):
            raise SnapshotManifestError(f"{cid}: QC snapshot not effectively FRESH")
        producer_row = producer_lanes.get(cid)
        if producer_row is None:
            raise SnapshotManifestError(f"{cid}: QC lane absent from manifest")
        expected_lineage = manifest_v1.lineage(producer_row)
        if (any(old.get(key) != value or new.get(key) != value
                for key, value in expected_lineage.items())):
            raise SnapshotManifestError(
                f"{cid}: QC producer lineage differs from governed M0")
        source_provenance[cid] = {
            "ingestion_snapshot_hash": saved_hash,
            "snapshot_manifest_sha256": current["snapshot_manifest_sha256"],
            "inventory_sha256": current["inventory_sha256"],
            "adapter_registry_source_sha256": current[
                "adapter_registry_source_sha256"],
            "adapter_registry_effective_sha256": current[
                "adapter_registry_effective_sha256"],
            **expected_lineage,
            "effective_freshness_state": "FRESH",
        }
    return source_provenance


def _governed_qc_postread(args, built: dict, manifest_hash: str,
                          latest_m0_hash: str, inventory_hash: str,
                          registry_hash: str) -> None:
    """Close the copy/manifest/read race before writing any QC output."""
    doc = manifest_v1.load_manifest(args.snapshot_manifest)
    if doc["_verified_manifest_sha256"] != manifest_hash:
        raise SnapshotManifestError("QC manifest changed during adapter read")
    try:
        actual_inventory_hash = "sha256:" + hashlib.sha256(
            args.inventory.read_bytes()).hexdigest()
    except OSError as exc:
        raise SnapshotManifestError("QC inventory unreadable during adapter read") from exc
    if actual_inventory_hash != inventory_hash:
        raise SnapshotManifestError("QC inventory changed during adapter read")
    registry_path = built.get("registry_path")
    if not isinstance(registry_path, Path):
        raise SnapshotManifestError("QC adapter registry path missing")
    try:
        actual_registry_hash = "sha256:" + hashlib.sha256(
            registry_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SnapshotManifestError("QC adapter registry unreadable during read") from exc
    if actual_registry_hash != registry_hash:
        raise SnapshotManifestError("QC adapter registry changed during read")
    manifest_v1.verify_adapter_identity(
        doc, args.expected_adapter_package_sha,
        args.expected_adapter_artifact_sha256)
    _, intake = manifest_v1.assess_intake(doc)
    lanes = {row["clank_id"]: row for row in doc["lanes"]}
    for cid in built["qc_adapters"]:
        row = lanes.get(cid)
        state = intake.get(cid)
        if (row is None or state is None
                or state["intake_freshness_state"] != "FRESH"
                or state["intake_child_execution_freshness"] != "FRESH"):
            raise SnapshotManifestError(f"{cid}: QC copy or child aged out during read")
        manifest_v1.verify_copy(row, Path(built["adapters"][cid].db_path))
    latest = syn.read_latest_snapshot(args.var_dir)
    if (not isinstance(latest, dict)
            or latest.get("content_hash") != latest_m0_hash
            or snap.content_hash({
                key: value for key, value in latest.items()
                if key != "content_hash"}) != latest_m0_hash):
        raise SnapshotManifestError("latest M0 harvest changed during QC read")


def _ingest_qc(args) -> int:
    try:
        built = build_adapters(args.real_state,
                               diagnostic_clank_path=args.adapters_src,
                               registry_path=args.adapter_registry)
    except AdapterPlaneUnavailable as exc:
        print(f"adapter plane unavailable: {exc}", file=sys.stderr)
        return 4
    # ingestion snapshot hash: reuse latest harvest snapshot if present
    latest = syn.read_latest_snapshot(args.var_dir) if hasattr(syn, "read_latest_snapshot") else None
    governed = (args.snapshot_manifest is not None
                or (latest or {}).get("snapshot_contract_version") == "1.0")
    source_provenance: dict[str, dict] = {}
    if governed:
        try:
            source_provenance = _governed_qc_gate(args, built, latest)
        except SnapshotManifestError as exc:
            print(f"governed QC rejected: {exc}", file=sys.stderr)
            return 6
    snap_hash = (latest or {}).get("content_hash", "no-snapshot")
    generated_from = (latest or {}).get("harvested_at_utc") \
        or datetime.now(UTC).isoformat(timespec="seconds")
    previous = qc.read_previous_qc_batch(args.out)
    blocks = {}
    for cid in built["qc_adapters"]:
        adapter = built["adapters"][cid]
        kwargs = {"ingestion_snapshot_hash": snap_hash}
        if governed:
            kwargs["source_snapshot"] = source_provenance[cid]
        blocks[cid] = qc.ingest_clank(cid, adapter, **kwargs)
        if governed and blocks[cid].get("observation") == "FAILED_ADAPTER":
            print(f"governed QC rejected: {cid} QC adapter failed",
                  file=sys.stderr)
            return 6
    if governed:
        try:
            _governed_qc_postread(
                args, built, latest["snapshot_manifest_sha256"], snap_hash,
                latest["inventory_sha256"],
                latest["adapter_registry_source_sha256"])
        except SnapshotManifestError as exc:
            print(f"governed QC rejected: {exc}", file=sys.stderr)
            return 6
    payload, warnings = qc.build_corpus(previous, blocks,
                                        generated_from=generated_from,
                                        snapshot_hash=snap_hash)
    if governed:
        # The merge may take time; do not append a batch citing inputs that
        # changed after the QC adapter returned.
        try:
            _governed_qc_postread(
                args, built, latest["snapshot_manifest_sha256"], snap_hash,
                latest["inventory_sha256"],
                latest["adapter_registry_source_sha256"])
        except SnapshotManifestError as exc:
            print(f"governed QC rejected: {exc}", file=sys.stderr)
            return 6
    if args.dry_run:
        print(qc.render_coverage(payload))
    else:
        target = qc.append_qc_batch(args.out, payload)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"qc-coverage-{payload['generated_from'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(qc.render_coverage(payload))
        print(f"qc-corpus: {target}")
        print(f"report:    {report}")
        print(f"records={payload['record_count']}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0

    if args.command == "harvest":
        return _harvest(args)
    if args.command == "synthesize":
        return _synthesize(args)
    if args.command == "detect":
        return _detect(args)
    if args.command == "recommend":
        return _recommend(args)
    return 2


def _soak_report(args) -> int:
    payload, warnings = soak.build_soak_report(args.var_dir, as_of=args.as_of)
    if args.dry_run:
        print(soak.render_soak(payload))
    else:
        target = soak.append_report(args.out, payload)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"qc-soak-{str(payload['window']['latest']).replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(soak.render_soak(payload))
        print(f"soak-report: {target}")
        print(f"report:      {report}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def _recommend(args) -> int:
    batch = recs.read_latest_anomaly_batch(args.var_dir)
    if batch is None:
        print(f"no anomaly batches under {args.var_dir / 'anomalies'}", file=sys.stderr)
        return 6
    recs_list = recs.derive_recommendations(batch)
    payload = recs.build_batch(args.out, batch, recs_list)
    if args.dry_run:
        print(render_recommendations(payload))
        print("--- recommendations payload ---")
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    else:
        target = recs.append_batch(args.out, payload)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"recommendations-{payload['generated_from'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_recommendations(payload))
        print(f"recommendations: {target}")
        print(f"report:          {report}")
        print(f"active={payload['active_count']} closed={payload['closed_count']}")
        # ADR-0003 §2: bridge into the Agent Inbox when an Inbox DB is given.
        # Local artifacts and Inbox delivery are separate outcomes, reported
        # independently; a bridge failure never masquerades as success.
        if args.inbox_db is not None:
            if args.inventory is None:
                print("inbox: DELIVERY FAILED: --inventory (fleet.yaml) is required "
                      "with --inbox-db; bridging refuses to run without canonical "
                      "membership data", file=sys.stderr)
                return 7
            try:
                from .registry_shim import operator_registry
                from .inbox_bridge import bridge_recommendations
                summary = bridge_recommendations(
                    payload, inbox_db_path=args.inbox_db,
                    registry=operator_registry(args.inventory),
                    diagnostic_clank_src=args.adapters_src,
                    rules_version=payload.get("rules_version", ""),
                )
            except Exception as exc:  # noqa: BLE001 — reported verbatim, not swallowed
                print(f"inbox: DELIVERY FAILED: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                print("(local recommendation artifacts were written successfully "
                      "before the failure; see paths above)", file=sys.stderr)
                return 7
            # Success line printed only after the ENTIRE batch completed.
            print(f"inbox:           delivered={len(summary['saved'])} "
                  f"deduplicated={summary['deduplicated']} "
                  f"producer={summary['misc_source']}")
    return 0

    if args.command == "harvest":
        return _harvest(args)
    if args.command == "synthesize":
        return _synthesize(args)
    if args.command == "detect":
        return _detect(args)
    return 2


def _detect(args) -> int:
    history = ano.load_history(args.var_dir)
    if not history:
        print(f"no snapshots under {args.var_dir / 'snapshots'}", file=sys.stderr)
        return 5
    events = _load_continuity(args.var_dir)
    expectations = _load_liveness(args.var_dir)
    traces = _load_scheduler_traces(args.var_dir)
    found = ano.detect(history, continuity_events=events or None,
                       liveness_expectations=expectations or None,
                       scheduler_traces=traces or None)
    batch = ano.build_batch(args.out, history, found)
    if args.dry_run:
        print(render_anomalies(batch))
        print("--- anomaly batch payload ---")
        print(json.dumps(batch, sort_keys=True, indent=2, default=str))
    else:
        target = ano.append_batch(args.out, batch)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"anomalies-{batch['batch_generated_from'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_anomalies(batch))
        print(f"anomalies: {target}")
        print(f"report:    {report}")
        print(f"active={batch['active_count']} recovered={batch['recovered_count']}")
    return 0


def _synthesize(args) -> int:
    payload = syn.read_latest_snapshot(args.var_dir)
    if payload is None:
        print(f"no snapshots found under {args.var_dir / 'snapshots'}", file=sys.stderr)
        return 5
    events = _load_continuity(args.var_dir)
    expectations = _load_liveness(args.var_dir)
    traces = _load_scheduler_traces(args.var_dir)
    synthesis = syn.synthesize_fleet(payload, stale_hours=args.stale_hours,
                                     continuity_events=events or None,
                                     liveness_expectations=expectations or None,
                                     scheduler_traces=traces or None)
    drift_rows = []
    if args.drift_checkouts:
        mapping = json.loads(args.drift_checkouts.read_text())
        inventory = json.loads(json.dumps({}))  # placeholder; ledger SHAs come from var inventory copy if present
        ledger = payload.get("inventory_ledger") or {}
        observed_at = payload.get("harvested_at_utc")
        for cid, checkout in mapping.items():
            drift_rows.append(drift_row(cid, Path(checkout),
                                        ledger.get(cid),
                                        observed_at))
    syn.attach_law9_drift(synthesis, drift_rows)
    synthesis["content_hash"] = syn.content_hash(synthesis)
    prev = syn.previous_synthesis_hash(args.out)
    synthesis["previous_synthesis_hash"] = prev

    if args.dry_run:
        print(render_synthesis(synthesis))
        print("--- synthesis payload ---")
        print(json.dumps(synthesis, sort_keys=True, indent=2, default=str))
    else:
        target = syn.append_synthesis(args.out, synthesis)
        rep_dir = args.out / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        report = rep_dir / f"fleet-synthesis-{synthesis['synthesized_at_utc'].replace(':', '').replace('+0000', 'Z')}.md"
        report.write_text(render_synthesis(synthesis))
        print(f"synthesis: {target}")
        print(f"report:    {report}")
    return 0


def _harvest(args) -> int:
    if not args.inventory.exists():
        print(f"inventory missing: {args.inventory}", file=sys.stderr)
        return 3
    try:
        built = build_adapters(args.real_state,
                               diagnostic_clank_path=args.adapters_src,
                               registry_path=getattr(args, "adapter_registry", None))
    except AdapterPlaneUnavailable as exc:
        print(f"adapter plane unavailable: {exc}", file=sys.stderr)
        return 4

    try:
        payload, warnings = snap.build_snapshot(
            inventory_path=args.inventory,
            adapters_result=built,
            real_state_dir=args.real_state,
            out_dir=args.out,
            continuity_events=_load_continuity(args.out) or None,
            snapshot_manifest_path=getattr(args, "snapshot_manifest", None),
            expected_adapter_package_sha=getattr(
                args, "expected_adapter_package_sha", None),
            expected_adapter_artifact_sha256=getattr(
                args, "expected_adapter_artifact_sha256", None),
        )
    except SnapshotManifestError as exc:
        print(f"snapshot manifest rejected: {exc}", file=sys.stderr)
        return 6

    if args.dry_run:
        print(render_report(payload))
        print("--- snapshot payload ---")
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    else:
        target = snap.append_snapshot(args.out, payload)
        report = write_report(args.out, payload)
        print(f"snapshot: {target}")
        print(f"report:   {report}")

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
