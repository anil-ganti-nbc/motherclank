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
}
for name, src in sources.items():
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst = dest / name
    dst_con = sqlite3.connect(dst)
    src_con.backup(dst_con)
    dst_con.close(); src_con.close()
    print("backed up:", name)
PYBACKUP
chmod 644 "$DEST"/*.db
# Hand directory back to the invoking user so unprivileged WAL readers can
# create their -shm sidecars (mode=ro on WAL requires directory write).
if [ -n "${SUDO_USER:-}" ]; then chown -R "$SUDO_USER" "$DEST"; fi
echo "refreshed: $DEST"
