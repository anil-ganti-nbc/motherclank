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
    # OEM Radar staging store ("data/" WAL sqlite). Inner filename not yet
    # operator-confirmed; line is guarded (SKIP until the path resolves).
    "oem_radar.db": "/var/lib/docker/volumes/oem_radar_staging_data/_data/oem_radar.db",
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
