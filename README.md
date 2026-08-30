# Motherclank M0

Read-only fleet harvester per [ADR-0002](https://github.com/anil-ganti-nbc/clank-architecture/blob/main/adr/0002-motherclank-supervisory-architecture.md).

```
motherclank harvest --inventory fleet.yaml --real-state DIR [--out DIR] [--dry-run] [--adapters-src PATH] [--cvc-root PATH]
```

- Consumes the Diagnostic-Clank-owned read-only adapters **unchanged** (located as
  workspace sibling `../diagnostic-clank`, or via `--adapters-src`).
- Onboarded order (Phase 2C): watch-clank → smartphone-clank → korean-tech-wire →
  feature-phone-clank.
- M1 `synthesize` derives per-Clank/fleet health (downgrade-only) and the Law 9
  drift metric; M2 `detect` maintains a deterministic anomaly ledger
  (transitions, blocked streaks, stale runs, scheduler invocation-vs-work,
  revision drift, fleet degradation; lifecycle NEW/ONGOING/RECOVERED).
- Emits append-only dated JSONL snapshots (`var/snapshots/YYYY-MM-DD.jsonl`) plus one
  derived Markdown report (`var/reports/fleet-*.md`). `--dry-run` writes nothing.
- UNKNOWN is preserved verbatim; missing data never becomes healthy/zero.
- Adapter failures are isolated to their Clank block; the fleet snapshot always completes.
- CVC Clank is an observer-tier institutional-memory lane. With `--cvc-root`,
  Motherclank reads its bounded corpus/integrity snapshot; CVC has no collector
  freshness, scheduled run, remediation, or participant authority.
- Every snapshot records inventory revision, adapter contract version,
  `previous_snapshot_hash`, own `content_hash`, and a sqlite `total_changes`
  read-only proof.

## Hard boundaries (ADR-0002)

No DB writes · no Clank mutations · no notifications · no locks on Clank stores ·
no shared databases · no remediation · no QC learning · no control actions.
The package source is scanned by tests for mutation/notification interfaces.

The canonical fleet manifest filters which registered lanes are harvested.
CVC's `RATIFIED_E4`, `SUPPORTED_E3`, `OPEN_TRIGGER`, `BLOCKED_EVIDENCE`, and
`HISTORICAL_EVIDENCE` values are evidence summaries, not fleet mandates. A
future Standards Clank may consume CVC evidence; Standards and the separate
editorial CVC Workbench are not started by this integration.

## Host operation (Hetzner)

```sh
git clone https://github.com/anil-ganti-nbc/motherclank.git ~/motherclank
cd ~/motherclank && python3 -m venv .venv && .venv/bin/pip install -e .
# refresh read-only copies (operator-authorized sudo cp; see script)
sudo -n ./scripts/refresh-real-state.sh ~/motherclank-real-state
.venv/bin/motherclank harvest \
  --inventory ../diagnostic-clank/clank-fleet/inventories/fleet.yaml \
  --real-state ~/motherclank-real-state --dry-run   # validate first
./scripts/install-user-timer.sh                       # ONE fixed-clock user timer
systemctl --user start motherclank-harvest.service    # first run on demand
```

`scripts/host-harvest.sh` (the unit's ExecStart) refreshes copies then harvests —
the sudo usage lives in this host wrapper only, never in the package.

## Rollback

```sh
systemctl --user disable --now motherclank-harvest.timer
rm -rf ~/.config/systemd/user/motherclank-harvest.{service,timer} && systemctl --user daemon-reload
rm -rf ~/motherclank ~/motherclank-real-state var/   # snapshots are disposable DERIVED data
```

Clank databases remain authoritative at every moment; nothing else needs reversal.
