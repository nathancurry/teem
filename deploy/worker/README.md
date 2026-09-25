# Worker host setup

The worker runs on a development machine, not on TrueNAS. Each coder, check, and review subprocess runs in a disposable rootless Podman container:

- The only writable mount is its workspace or scratch directory. Git metadata and the contract are mounted read-only, and the root filesystem is read-only.
- Its only network is an internal Podman network with no DNS. The only other member of that network is an allowlisting proxy, reached by a fixed IP.
- It receives only the environment Teem sets explicitly. Host environment variables and the worker's credentials are not passed in.

A container does not die with the worker. If the worker crashes, its running container continues until its Podman timeout. On the next start, the worker removes containers labeled with that attempt before reconciling it, so no attempt ever runs twice.

## One-time setup

Run these commands as the user that runs `teem-worker`. Rootless Podman needs cgroup v2 with memory, CPU, and PID controllers delegated to that user; recent Fedora and Debian releases do this by default.

```sh
cd deploy/worker
podman build -t localhost/teem-agent:1 -f agent.Containerfile .
podman build -t localhost/teem-proxy:1 -f proxy.Containerfile .
podman network create --internal --disable-dns --subnet 10.203.7.0/24 teem-agents
mkdir -p ~/.config/teem && cp allowlist.example ~/.config/teem/allowlist
podman run -d --name teem-proxy --restart always \
  --network podman --network teem-agents:ip=10.203.7.2 \
  -v ~/.config/teem/allowlist:/etc/tinyproxy/allow:ro,z localhost/teem-proxy:1
```

The allowlist holds one extended regular expression per line, matched against the host of each HTTPS tunnel. Add the registries your projects' checks and installs need. Everything else, including LAN addresses and plain HTTP, is refused. Denials appear in `podman logs teem-proxy`. To keep the proxy running across reboots, generate a systemd user unit or use a Quadlet, and enable lingering for the user.

The worker's policy file names the image, network, and proxy:

```json
{
  "owners": ["your-github-user"],
  "token": "<optional read-only token for private repositories>",
  "image": "localhost/teem-agent:1",
  "network": "teem-agents",
  "proxy": "http://10.203.7.2:8888",
  "coder": ["..."],
  "reviewer": {"...": "see the main README"}
}
```

Rebuild the agent image deliberately to update the pinned CLI versions, give it a new tag, and change `image` in the policy file.
