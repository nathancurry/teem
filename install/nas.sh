#!/usr/bin/env bash
# TrueNAS SCALE: datasets, secrets, backup, image, migrations, server.
# Run as root from the cloned repo. Safe to re-run; also how updates are deployed:
#   git -C /mnt/storage/teem/repo fetch --tags && git -C /mnt/storage/teem/repo checkout vX.Y.Z
#   sudo /mnt/storage/teem/repo/install/nas.sh
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root

say "Checking Docker"
if ! docker info >/dev/null 2>&1; then
    # Manual: picking the Apps pool is a storage decision for the operator.
    die "Docker isn't running. In the TrueNAS UI: Apps -> Configuration -> Choose Pool. Then re-run."
fi

say "Checking for an older Teem app"
# The earlier deployment was a TrueNAS "Install via YAML" app on the same port and data.
other="$(docker ps --format '{{.Names}} {{.Ports}}' | awk -v port=":$NAS_PORT->" \
         'index($0, port) && $1 != "teem-server" {print $1}')"
if [ -n "$other" ]; then
    die "container '$other' already serves port $NAS_PORT. If it's the old Teem app, stop and delete it in
       Apps (the data under $NAS_ROOT stays), then re-run."
fi
# Two PostgreSQL servers on one data directory corrupt it, so nothing else may have it mounted.
for id in $(docker ps -q); do
    name="$(docker inspect -f '{{.Name}}' "$id" | tr -d /)"
    if [ "$name" != teem-postgres ] && docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$id" |
            grep -qx "$NAS_POSTGRES"; then
        die "container '$name' is using $NAS_POSTGRES. Stop and delete the old Teem app in Apps, then re-run."
    fi
done

say "Datasets under $NAS_POOL_DATASET"
# midclt (not raw zfs) so the TrueNAS UI knows about them. Existing directories are kept as they are.
for sub in "" /config /postgres /artifacts /speech /backups; do
    ds="$NAS_POOL_DATASET$sub"
    path="$NAS_ROOT$sub"
    if zfs list -H -o name "$ds" >/dev/null 2>&1 || [ -d "$path" ]; then
        echo "exists: $path"
    else
        midclt call pool.dataset.create "{\"name\": \"$ds\"}" >/dev/null
        echo "created: $ds"
    fi
done
for path in "$NAS_CONFIG" "$NAS_POSTGRES" "$NAS_ARTIFACTS" "$NAS_SPEECH" "$NAS_BACKUPS"; do
    [ -d "$path" ] || die "$path is not mounted"
done

# The server runs as the apps user: it reads config and owns artifacts. Only root reads backups.
chown "root:$APPS_GID" "$NAS_CONFIG" && chmod 750 "$NAS_CONFIG"
chown "$APPS_UID:$APPS_GID" "$NAS_ARTIFACTS" && chmod 700 "$NAS_ARTIFACTS"
chown "root:$APPS_GID" "$NAS_SPEECH" && chmod 750 "$NAS_SPEECH"
chown root:root "$NAS_BACKUPS" && chmod 700 "$NAS_BACKUPS"

say "Secrets in $NAS_CONFIG"
db_initialized=""
[ -n "$(ls -A "$NAS_POSTGRES" 2>/dev/null)" ] && db_initialized=yes
if [ ! -s "$NAS_CONFIG/postgres_password" ]; then
    # PostgreSQL only reads this file when it creates a new data directory.
    [ -z "$db_initialized" ] || die "$NAS_POSTGRES has data but $NAS_CONFIG/postgres_password is missing; restore it."
    (umask 077; random_secret > "$NAS_CONFIG/postgres_password")
fi
db_password="$(tr -d '\n' < "$NAS_CONFIG/postgres_password")"
db_password_encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$db_password")"
env_ensure "$NAS_ENV" TEEM_DSN "postgresql://teem:$db_password_encoded@postgres:5432/teem"
env_ensure "$NAS_ENV" TEEM_USER operator
env_ensure "$NAS_ENV" TEEM_PASSWORD "$(random_secret)"
env_ensure "$NAS_ENV" TEEM_WORKER_TOKEN "$(random_secret)"
env_ensure "$NAS_ENV" TEEM_CHAT_KEY "$(random_secret)"
[ -n "$(env_get "$NAS_ENV" TEEM_ORIGIN)" ] || die "add the server's HTTPS address to $NAS_ENV, for example:
       TEEM_ORIGIN=https://truenas.example.ts.net:8443
       Then re-run."
chmod 600 "$NAS_ENV"
chown root:root "$NAS_ENV" "$NAS_CONFIG/postgres_password"
chmod 600 "$NAS_CONFIG/postgres_password"

say "Configuration files"
missing=""
for name in telegram github decider reviewer; do
    file="$NAS_CONFIG/$name.json"
    if [ ! -f "$file" ]; then
        cp "$REPO_DIR/deploy/truenas/$name.example.json" "$file"
        [ "$name" = reviewer ] || missing="$missing $file"
    fi
    chown "root:$APPS_GID" "$file" && chmod 640 "$file"
done
if [ -f "$NAS_CONFIG/speech.json" ]; then
    chown "root:$APPS_GID" "$NAS_CONFIG/speech.json" && chmod 640 "$NAS_CONFIG/speech.json"
    # speech.json turns voice on; the server refuses to start if its runner or model is missing.
    for path in $(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["executable"], c["model"])' \
                  "$NAS_CONFIG/speech.json"); do
        host_path="$NAS_SPEECH${path#/srv/teem/speech}"
        [ -f "$host_path" ] || die "speech.json turns voice on, but $host_path isn't installed. Install it, or
       disable voice with: mv $NAS_CONFIG/speech.json $NAS_CONFIG/speech.json.disabled"
    done
fi
[ -z "$missing" ] || die "fill in these example files, then re-run:$missing"

if [ -n "$db_initialized" ] && docker ps --format '{{.Names}}' | grep -qx teem-postgres; then
    say "Backing up before changing anything"
    "$REPO_DIR/install/backup.sh"
fi

say "Building the server image from $(git -C "$REPO_DIR" describe --tags --always --dirty)"
"${COMPOSE[@]}" --profile tools build

say "Starting PostgreSQL and applying migrations"
"${COMPOSE[@]}" up -d postgres
"${COMPOSE[@]}" run --rm migrate

say "Starting the server"
"${COMPOSE[@]}" up -d server

say "Verifying the server redirects to sign-in"
wait_for_status "http://127.0.0.1:$NAS_PORT/" 303 60

cat <<EOF

Teem $(git -C "$REPO_DIR" describe --tags --always --dirty) is up on 127.0.0.1:$NAS_PORT.
- Sign in as $(env_get "$NAS_ENV" TEEM_USER); the password is TEEM_PASSWORD in $NAS_ENV.
- The worker needs TEEM_WORKER_TOKEN from $NAS_ENV in its config/worker-token file.
- Voice apps (OpenAI-compatible): base URL $(env_get "$NAS_ENV" TEEM_ORIGIN)/v1, model "teem",
  API key TEEM_CHAT_KEY from $NAS_ENV.
- Run install/backup.sh daily: TrueNAS UI -> System -> Advanced -> Cron Jobs, as root:
    $REPO_DIR/install/backup.sh
- Then update the worker to the same version with install/worker.sh.
EOF
