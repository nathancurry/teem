#!/usr/bin/env bash
# Worker host: install or update teem-worker for the dedicated worker user. Safe to re-run; also how
# updates are deployed, at the same version as the server:
#   git -C ~/src fetch --tags && git -C ~/src checkout vX.Y.Z && ~/src/install/worker.sh
# Run as that user in a real login session: sudo machinectl shell teem@
set -euo pipefail
source "$(dirname "$0")/lib.sh"

BIN="$HOME/bin"
CONFIG="$HOME/config"
STATE="$HOME/state"
UNIT="$HOME/.config/systemd/user/teem-worker.service"

[ "$(id -u)" -ne 0 ] || die "run as the worker user (sudo machinectl shell teem@), not root"
for tool in podman uv git python3; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
done
systemctl --user show-environment >/dev/null 2>&1 ||
    die "no user session; log in with: sudo machinectl shell $USER@"
loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes ||
    die "the worker must keep running when you log out: sudo loginctl enable-linger $USER"

say "Configuration in $CONFIG"
mkdir -p "$BIN" "$CONFIG" "$STATE" "$HOME/codex-home" "$(dirname "$UNIT")"
chmod 700 "$CONFIG" "$STATE"
[ -f "$CONFIG/allowlist" ] || cp "$REPO_DIR/deploy/worker/allowlist.example" "$CONFIG/allowlist"
url="$(env_get "$CONFIG/worker.env" TEEM_URL)"
[ -n "$url" ] || die "put the server address in $CONFIG/worker.env, for example:
       TEEM_URL=https://truenas.example.ts.net:8443"
[ -s "$CONFIG/worker-token" ] || die "put TEEM_WORKER_TOKEN from the NAS's teem.env into $CONFIG/worker-token"
[ -f "$CONFIG/policy.json" ] || die "write $CONFIG/policy.json (see the README's worker policy example), then re-run"
chmod 600 "$CONFIG/worker-token" "$CONFIG/policy.json"

running="$(podman ps -q --filter label=teem.attempt)"
if [ -n "$running" ]; then
    echo "An Attempt is running now. Restarting the worker stops it, and its Run will need a retry."
    confirm "Restart anyway?"
fi

say "Installing teem-worker from $(git -C "$REPO_DIR" describe --tags --always --dirty)"
uv tool install --force --quiet "$REPO_DIR"
install -m 755 "$REPO_DIR/teem/reviewer_codex.py" "$BIN/reviewer-codex"
install -m 755 "$REPO_DIR/teem/reviewer_claude.py" "$BIN/reviewer-claude"

say "Agent and proxy images"
agent="localhost/teem-agent:$(content_tag deploy/worker/agent.Containerfile deploy/worker/teem-implement)"
proxy="localhost/teem-proxy:$(content_tag deploy/worker/proxy.Containerfile deploy/worker/tinyproxy.conf)"
for image in "$agent" "$proxy"; do
    if podman image exists "$image"; then
        echo "exists: $image"
    else
        name="${image#localhost/teem-}"
        podman build -q -t "$image" -f "$REPO_DIR/deploy/worker/${name%%:*}.Containerfile" "$REPO_DIR/deploy/worker"
        echo "built: $image"
    fi
done

say "Agent network and proxy"
podman network exists "$AGENT_NETWORK" ||
    podman network create --internal --disable-dns --subnet "$AGENT_SUBNET" "$AGENT_NETWORK" >/dev/null
# Recreated on every run so a new image or an edited allowlist takes effect.
podman rm -f -t 0 teem-proxy >/dev/null 2>&1 || true
podman run -d --name teem-proxy --restart always \
    --network podman --network "$AGENT_NETWORK:ip=$PROXY_IP" \
    -v "$CONFIG/allowlist:/etc/tinyproxy/allow:ro,z" "$proxy" >/dev/null
systemctl --user enable --quiet podman-restart.service

say "Pointing the worker policy at this version's image and network"
python3 - "$CONFIG/policy.json" "$agent" "$AGENT_NETWORK" "http://$PROXY_IP:8888" <<'PY'
import json, sys
path, image, network, proxy = sys.argv[1:]
with open(path) as source:
    policy = json.load(source)
if (policy.get("image"), policy.get("network"), policy.get("proxy")) != (image, network, proxy):
    policy.update(image=image, network=network, proxy=proxy)
    with open(path, "w") as target:
        json.dump(policy, target, indent=2)
        target.write("\n")
    print("updated image, network, and proxy")
PY

say "Service"
cat > "$UNIT" <<EOF
[Unit]
Description=Teem worker
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$HOME/.local/bin/teem-worker --url $url --token-file $CONFIG/worker-token --worker-id worker-1 --projects $CONFIG/policy.json --state-dir $STATE
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --quiet teem-worker
systemctl --user restart teem-worker
sleep 5
systemctl --user is-active --quiet teem-worker || die "teem-worker did not stay up; see: journalctl --user -u teem-worker"
if journalctl --user -u teem-worker --since "-10s" --no-pager | grep -q "worker: "; then
    journalctl --user -u teem-worker --since "-10s" --no-pager | grep "worker: " | tail -3
    die "teem-worker is running but reporting errors (above)"
fi

cat <<EOF

teem-worker $(git -C "$REPO_DIR" describe --tags --always --dirty) is running and polling $url.
- Logs: journalctl --user -u teem-worker -f
- Proxy denials: podman logs teem-proxy 2>&1 | grep refused
EOF
