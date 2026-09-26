# shellcheck shell=bash
# Shared helpers for install scripts. Source this; don't run it.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# NAS layout (TrueNAS SCALE, pool "storage").
NAS_POOL_DATASET="storage/teem"
NAS_ROOT="/mnt/storage/teem"
NAS_CONFIG="$NAS_ROOT/config"
NAS_POSTGRES="$NAS_ROOT/postgres"
NAS_ARTIFACTS="$NAS_ROOT/artifacts"
NAS_SPEECH="$NAS_ROOT/speech"
NAS_BACKUPS="$NAS_ROOT/backups"
NAS_ENV="$NAS_CONFIG/teem.env"
NAS_PORT=8765
APPS_UID=568
APPS_GID=568
COMPOSE=(docker compose -f "$REPO_DIR/install/compose.yaml")

# Worker layout: the dedicated worker user's home.
AGENT_NETWORK="teem-agents"
AGENT_SUBNET="10.203.7.0/24"
PROXY_IP="10.203.7.2"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    local reply
    read -rp "$1 [y/N] " reply
    case "$reply" in
        y|Y|yes) return 0 ;;
        *) die "stopped; nothing further was changed" ;;
    esac
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
}

# Env files are plain KEY=VALUE lines, readable by both bash and compose env_file.
env_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

# Appends KEY only if it's absent, so re-runs never rotate existing values.
env_ensure() {
    local file="$1" key="$2" value="$3"
    [ -n "$(env_get "$file" "$key")" ] && return 0
    [ -n "$value" ] || die "empty value for $key"
    (umask 077; touch "$file")
    printf '%s=%s\n' "$key" "$value" >> "$file"
    chmod 600 "$file"
}

random_secret() {
    openssl rand -hex 32
}

# Waits until a GET answers with the expected HTTP status.
wait_for_status() {
    local url="$1" expected="$2" seconds="$3" code=""
    for _ in $(seq "$seconds"); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
        [ "$code" = "$expected" ] && return 0
        sleep 1
    done
    die "expected $expected from $url, last got '$code'"
}

# A short hash of the files an image is built from, so a changed Containerfile or wrapper
# produces a new tag and an unchanged one reuses the existing image.
content_tag() {
    (cd "$REPO_DIR" && cat "$@" | sha256sum | cut -c1-12)
}
