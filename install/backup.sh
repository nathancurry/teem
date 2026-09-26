#!/usr/bin/env bash
# TrueNAS SCALE: back up Teem's database, artifacts, and configuration. Run as root.
# nas.sh runs it before every deploy; schedule it daily as a TrueNAS cron job too.
# Keeps the newest $TEEM_BACKUPS_KEEP backups (default 14) in /mnt/storage/teem/backups.
#
# Backups contain secrets (config) and your code (artifacts). They live on the same pool as the
# data, so they protect against mistakes, not disk loss: replicate or copy the backups dataset
# off the NAS (TrueNAS: Data Protection -> Replication or Cloud Sync) for that.
#
# To restore backup DIR:
#   docker stop teem-server
#   docker exec -i teem-postgres pg_restore -U teem -d teem --clean --if-exists < DIR/teem.dump
#   tar -xzf DIR/artifacts.tar.gz -C /mnt/storage/teem
#   tar -xzf DIR/config.tar.gz -C /mnt/storage/teem     # only if the config was lost
#   docker start teem-server
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root
keep="${TEEM_BACKUPS_KEEP:-14}"
docker ps --format '{{.Names}}' | grep -qx teem-postgres || die "teem-postgres isn't running; nothing to back up from"

dir="$NAS_BACKUPS/$(date +%Y-%m-%dT%H%M%S)"
say "Backing up to $dir"
(umask 077; mkdir -p "$dir")
# A logical dump is consistent while the server keeps running, unlike copying the data directory.
docker exec teem-postgres pg_dump -U teem -d teem -Fc > "$dir/teem.dump"
# Repository mirrors are a cache of GitHub, rebuilt on demand.
tar -czf "$dir/artifacts.tar.gz" -C "$NAS_ROOT" --exclude=artifacts/mirrors artifacts
tar -czf "$dir/config.tar.gz" -C "$NAS_ROOT" config
chmod 600 "$dir"/*
du -sh "$dir" | cut -f1 | xargs printf 'size: %s\n'

say "Keeping the newest $keep backups"
find "$NAS_BACKUPS" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n "-$keep" | while read -r old; do
    rm -rf "$old"
    echo "removed: $old"
done
