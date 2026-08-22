#!/bin/sh
# systemd user unit ExecStart for Motherclank M0 (ADR-0002).
# Refreshes read-only copies (sudo cp only) then runs the pure harvester.
set -eu
HOME_DIR="$(dirname "$(dirname "$0")")"
REAL_STATE="$HOME_DIR/real-state"
sudo -n "$HOME_DIR/scripts/refresh-real-state.sh" "$REAL_STATE"
cd "$HOME_DIR"
exec .venv/bin/motherclank harvest \
  --inventory ../diagnostic-clank/clank-fleet/inventories/fleet.yaml \
  --real-state "$REAL_STATE" \
  --out var \
  --adapters-src ../diagnostic-clank
