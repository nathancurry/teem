# Worker host

The worker runs on a development machine, not on TrueNAS. Each coder, check, and review subprocess runs in a disposable rootless Podman container:

- The only writable mount is its workspace or scratch directory. Git metadata and the contract are mounted read-only, and the root filesystem is read-only.
- Its only network is an internal Podman network with no DNS. The only other member of that network is an allowlisting proxy, reached by a fixed IP.
- It receives only the environment Teem sets explicitly. Host environment variables and the worker's credentials are not passed in.

A container does not die with the worker. If the worker crashes, its running container continues until its Podman timeout. On the next start, the worker removes containers labeled with that attempt before reconciling it, so no attempt ever runs twice.

[`install/worker.sh`](../../install/worker.sh) builds the images in this directory, creates the network and proxy, and installs the service. The one-time setup below needs root, so the script doesn't do it.

## One-time user setup (as root)

A dedicated user keeps the agents' containers and credentials apart from your own account. Rootless Podman needs cgroup v2 with memory, CPU, and PID controllers delegated to the user; recent Fedora and Debian releases do this by default.

```sh
sudo useradd -m -d /srv/teem -s /bin/bash teem
sudo loginctl enable-linger teem          # keeps the worker running without a login
# SELinux (Fedora): label /srv/teem like a home directory, including Podman's storage.
sudo semanage fcontext -a -e /home/$USER /srv/teem && sudo restorecon -RF /srv/teem
```

Then log in as that user with `sudo machinectl shell teem@` (not `sudo -u`, which lacks the user session that `systemctl --user` and Podman need), and follow the worker steps in the [main README](../../README.md#worker-setup).

## Credentials

- **Claude Code:** run `claude setup-token` anywhere you are signed in, and put the token in the coder's `env` in `policy.json`.
- **Codex:** sign in once into the reviewer's home directory:
  ```sh
  podman run --rm -it -v /srv/teem/codex-home:/home/agent:z -e HOME=/home/agent \
    --userns=keep-id <agent image from policy.json> codex login --device-auth
  ```

## Allowlist

`~/config/allowlist` holds one extended regular expression per line, matched against the host of each HTTPS tunnel. Add the registries your projects' checks and installs need; everything else, including LAN addresses and plain HTTP, is refused. Re-run `install/worker.sh` after editing it. Denials appear in `podman logs teem-proxy 2>&1 | grep refused`.
