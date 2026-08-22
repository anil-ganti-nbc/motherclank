#!/bin/sh
# systemd user unit ExecStart for Motherclank M0 (ADR-0002).
# Refreshes read-only copies (sudo cp only) then runs the pure harvester.
set -eu
HOME_DIR="$(dirname "$(dirname "$0")")"
REAL_STATE="$HOME_DIR/real-state"
sudo -n "$HOME_DIR/scripts/refresh-real-state.sh" "$REAL_STATE"
cd "$HOME_DIR"
.venv/bin/motherclank harvest \
  --inventory ../diagnostic-clank/clank-fleet/inventories/fleet.yaml \
  --real-state "$REAL_STATE" \
  --out var \
  --adapters-src ../diagnostic-clank

# M1: derive fleet health from the freshest snapshot (Law 9 checkouts included)
.venv/bin/motherclank synthesize \
  --var-dir var \
  --out var \
  --stale-hours 24 \
  --drift-checkouts scripts/law9-checkouts.json

# M2: deterministic anomaly ledger over accumulated history
.venv/bin/motherclank detect --var-dir var --out var

# M3: advisory operator recommendations from the anomaly ledger
.venv/bin/motherclank recommend --var-dir var --out var
