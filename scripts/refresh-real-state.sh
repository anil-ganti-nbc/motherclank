#!/bin/sh
# Operator-authorized helper: refresh READ-ONLY copies of live Clank DBs.
# Copies only; never mutates sources. Usage: sudo ./refresh-real-state.sh DEST
set -eu
DEST="${1:?destination dir}"
mkdir -p "$DEST"
cp /var/lib/docker/volumes/watch_clank_staging_data/_data/watch_clank.db "$DEST/"
cp /opt/smartphone-clank/data/clank.db "$DEST/smartphone_clank.db"
cp /opt/korean-tech-wire/var/korean_tech_wire.db "$DEST/korean_tech_wire.db"
cp /var/lib/docker/volumes/feature_phone_clank_staging_data/_data/feature_phone_clank.db "$DEST/feature_phone_clank.db"
chmod 644 "$DEST"/*.db
echo "refreshed: $DEST"
