#!/bin/sh
# Operator-authorized helper: refresh READ-ONLY, CONSISTENT copies of live
# Clank DBs using the SQLite online-backup API (safe against hot WAL).
# Copies only; never mutates sources. Usage: sudo ./refresh-real-state.sh DEST [VENV_PYTHON]
set -eu
DEST="${1:?destination dir}"
PY="${2:-python3}"
mkdir -p "$DEST"
"$PY" - "$DEST" <<'PYBACKUP'
import sqlite3, sys, pathlib
dest = pathlib.Path(sys.argv[1])
sources = {
    "watch_clank.db": "/var/lib/docker/volumes/watch_clank_staging_data/_data/watch_clank.db",
    "smartphone_clank.db": "/opt/smartphone-clank/data/clank.db",
    "korean_tech_wire.db": "/opt/korean-tech-wire/var/korean_tech_wire.db",
    "feature_phone_clank.db": "/var/lib/docker/volumes/feature_phone_clank_staging_data/_data/feature_phone_clank.db",
    # Smartwatch (restored volume). Inner filename per incident evidence
    # (/app/data/smartwatch-clank.sqlite3); operator: confirm the volume's
    # host-side inner path once, then this line goes live.
    "smartwatch-clank.sqlite3": "/var/lib/docker/volumes/smartwatch_clank_staging_data/_data/smartwatch-clank.sqlite3",
    # OEM Radar. Operator-confirmed 2026-08-24 against the live volume:
    # docker-compose.yml (Tier C/portability, "NOT a production deployment")
    # names the volume "oem_radar_portability_data" (not *_staging_data as
    # guessed), mounted at /app/data; the inner file is "radar.db" (not
    # "oem_radar.db"). Both names below are copied verbatim from that
    # verification, not inferred from the resource's own naming pattern.
    "radar.db": "/var/lib/docker/volumes/oem_radar_portability_data/_data/radar.db",
    # Free Game Tracker. Operator-confirmed 2026-08-24 against the live
    # volume: docker-compose.yml names the volume "fgt_production_data"
    # (mounted /app/data), NEWSROOM_DATABASE_PATH=/app/data/newsroom.db.
    # This deploy directory is NOT a git checkout (project doesn't use
    # GitHub yet per its own compose comment); .deployed-id is a plain
    # identifier, not a commit SHA - do not assume git ancestry here.
    "newsroom.db": "/var/lib/docker/volumes/fgt_production_data/_data/newsroom.db",
}
import os
for name, src in sources.items():
    if not os.path.exists(src):
        print("SKIP (source missing):", name)
        continue
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst = dest / name
    dst_con = sqlite3.connect(dst)
    src_con.backup(dst_con)
    dst_con.close(); src_con.close()
    print("backed up:", name)
PYBACKUP
find "$DEST" -maxdepth 1 -type f -name '*.db' -exec chmod 644 {} +
find "$DEST" -maxdepth 1 -type f -name '*.sqlite3' -exec chmod 644 {} +
# Hand directory back to the invoking user so unprivileged WAL readers can
# create their -shm sidecars (mode=ro on WAL requires directory write).
if [ -n "${SUDO_USER:-}" ]; then chown -R "$SUDO_USER" "$DEST"; fi
echo "refreshed: $DEST"
