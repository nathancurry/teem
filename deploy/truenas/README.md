# TrueNAS SCALE deployment

This is a single-host example for a Docker-based TrueNAS SCALE release with **Install via YAML**. Replace `tank`, `teem.example.com`, and all example credentials before pasting the two Compose files into separate apps. The Teem server serves the PWA files from its installed package and runs the push sender in the same process; neither needs another container. Workers and their repositories stay on other machines. No GPU or inference service is part of these YAML files. TrueNAS documents [Install via YAML](https://apps.truenas.com/managing-apps/installing-custom-apps/) as a Docker Compose editor.

## 1. Host storage and access

Create these paths before installing either app. Use a pool name in place of `tank`, then make the same replacement in both YAML files and `env.example`.

| Host path | Container path | Access and owner |
| --- | --- | --- |
| `/mnt/tank/teem/postgres` | `/var/lib/postgresql/data` | PostgreSQL image's `postgres` user, private read/write; empty on first boot |
| `/mnt/tank/teem/artifacts` | `/srv/teem/artifacts` | UID/GID 568, private read/write |
| `/mnt/tank/teem/config` | `/srv/teem/config` | UID 568 can read; only an administrator can change files |
| `/mnt/tank/teem/caddy/Caddyfile` | `/etc/caddy/Caddyfile` | Readable by Caddy |
| `/mnt/tank/teem/caddy/data` | `/data` | Caddy can write; stores ACME certificates |
| `/mnt/tank/teem/caddy/config` | `/config` | Caddy can write |

Use private dataset ACLs, not a public share. The PostgreSQL image manages ownership of an empty data directory; confirm its runtime user can write it before starting. Set the artifact dataset's owner to 568:568. Make `teem.env`, `postgres_password`, `vapid.pem`, and `reviewer.json` readable only by their required container user and administrators. A source dataset mounted read-only in the container does not replace host-side ACLs. Keep the PostgreSQL data and artifacts on durable storage with enough free space for 50 MiB candidate bundles and normal PostgreSQL growth.

Put the current [`teem/schema.sql`](../../teem/schema.sql) at `/mnt/tank/teem/config/schema.sql` and copy [`env.example`](env.example) to `/mnt/tank/teem/config/teem.env`. Create `/mnt/tank/teem/config/postgres_password` as a single-line file containing the database password. Put the same password, URL-encoded where needed, into `TEEM_DSN`. The Compose file uses Docker's `POSTGRES_PASSWORD_FILE`; Teem itself receives the DSN as its existing `--dsn` option. `TEEM_*` names in `teem.env` are used by the Compose shell wrapper, not read by Teem as application settings. Keep the env file out of source control. Docker process arguments and container metadata can expose CLI credentials to TrueNAS administrators; restrict host and app management access accordingly.

Create `/mnt/tank/teem/config/reviewer.json` using the server reviewer JSON in the [main README](../../README.md). Its identity, instructions hash, destination, model, and timeout must match worker policy. After building the image below, generate one VAPID private key, then keep that exact key through upgrades and restore it with the secrets:

```sh
docker run --rm --entrypoint python -v /mnt/tank/teem/config:/srv/teem/config teem:0.1.0 \
  -c 'from py_vapid import Vapid02; v=Vapid02(); v.generate_keys(); v.save_key("/srv/teem/config/vapid.pem")'
chmod 600 /mnt/tank/teem/config/vapid.pem
```

Set `TEEM_VAPID_SUBJECT` to a real `mailto:` contact. The push sender starts automatically when both VAPID options are passed; there is no separate push command. Its outbound HTTPS/DNS access must reach `fcm.googleapis.com`, `updates.push.services.mozilla.com`, and `*.push.apple.com`.

## 2. Teem image and database

Build the repository's root [`Dockerfile`](../../Dockerfile) on the TrueNAS host from the checked-out revision. Its `server` target installs Teem and the PWA from `pyproject.toml` and `uv.lock`, plus Git for server-side candidate inspection. It runs as UID/GID 568. The default image has no FFmpeg, Bubblewrap, whisper executable, or model. From the repository root, build the tag used in `teem.compose.yaml`:

```sh
docker build --target server -t teem:0.1.0 .
docker run --rm teem:0.1.0 serve --help
```

The image contains no credentials or deployment data; the Compose file still bind-mounts artifacts and read-only configuration. For a controlled upgrade, build from a known Teem commit, retain the previous image, and pin base images to reviewed digests in your local deployment. `pull_policy: never` prevents TrueNAS from silently replacing this local Teem image. A small [GitHub Actions workflow](../../.github/workflows/image.yaml) also publishes the default server target to `ghcr.io/nathancurry/teem:vX.Y.Z` when a `vX.Y.Z` tag is pushed. To use that registry image, change the Compose image reference and remove `pull_policy: never`; configure GHCR access on TrueNAS if the package is private. The Compose example uses the existing `teem-server serve` CLI and binds its internal listener to `0.0.0.0` only inside the container. Docker publishes port 8765 on the TrueNAS host's `127.0.0.1` only; PostgreSQL has no published port. Keep the server to one replica because the current implementation embeds its sweep and push loops in that process.

The official PostgreSQL image creates database/user `teem` on an **empty** data directory and runs the mounted `schema.sql` once through `/docker-entrypoint-initdb.d`. The server waits for `pg_isready`, which checks database availability but is not an application health endpoint. The current `teem-server init --dsn ...` executes the same [`teem/schema.sql`](../../teem/schema.sql); run it **only for a fresh database** when not using the first-boot mount. It is not a migration tool, and `schema.sql` includes non-idempotent constraint additions. Existing slice-1 databases require manual inspection and SQL alteration. For an existing slice-2 database, apply [`docs/architecture/slice-3.sql`](../../docs/architecture/slice-3.sql) exactly once with a PostgreSQL client before starting this server. That SQL adds push subscriptions and delivery rows; it does not backfill old events. A restored database already containing the current schema must not run either SQL again.

## 3. HTTPS and startup

Copy [`Caddyfile.example`](Caddyfile.example) to `/mnt/tank/teem/caddy/Caddyfile` and replace its hostname. Set `TEEM_ORIGIN` to exactly `https://` plus that hostname, without a trailing slash. Give the hostname working DNS and certificate issuance reachability. Caddy's host networking needs host TCP ports 80 and 443; move the TrueNAS management UI off those ports first and restrict its new port separately. Install `teem.compose.yaml` as one app, confirm PostgreSQL initialization and Teem startup, then install `caddy.compose.yaml` as a second app. The host-network Caddy container connects to the loopback-only published Teem port. Caddy obtains and renews certificates and persists them under `caddy/data`. See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) for DNS and challenge requirements.

The Caddyfile proxies the whole origin, including `/sw.js` at the root so it can control the PWA. It preserves incoming `Authorization` and `Origin` headers; Teem requires Basic authentication and same-origin browser POSTs, while workers use a bearer token. Teem already returns `Cache-Control: no-store` on its responses, including static assets. Caddy limits `/transcribe` requests to 8 MiB and allows 120 seconds to read the upstream response, covering Teem's 90-second inference bound. Do not put a site-wide 8 MiB limit in front of `/worker/upload/`, which accepts candidate bundles up to 50 MiB. The browser has a 30-second upload timeout. [Caddy's `request_body` directive](https://caddyserver.com/docs/caddyfile/directives/request_body) requires Caddy 2.10 or later.

## 4. Optional local dictation

The example starts with voice disabled: it passes neither speech option. `/transcribe` then returns 503 after authentication. For voice, explicitly build the Dockerfile's `speech` target, which adds FFmpeg/ffprobe and Bubblewrap, and change the server image in `teem.compose.yaml` to `teem:0.1.0-speech`:

```sh
docker build --target speech -t teem:0.1.0-speech .
```

Install a static `whisper-cli` built from whisper.cpp v1.9.4 and the `ggml-base.en.bin` model in the private config dataset, readable by UID 568. Neither is baked into either image. The model SHA-256 is `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`; calculate the actual executable SHA-256. Put this JSON in `/mnt/tank/teem/config/speech.json`:

```json
{
  "executable": "/srv/teem/config/whisper-cli",
  "executable_sha256": "<SHA-256 of the installed static executable>",
  "model": "/srv/teem/config/ggml-base.en.bin",
  "model_sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
  "language": "en"
}
```

Add `--speech-config /srv/teem/config/speech.json --speech-scratch /tmp/teem-speech-scratch` to the server command in `teem.compose.yaml` and redeploy. `/tmp/teem-speech-scratch` is container-local, outside artifact storage and backups; Teem creates it and clears abandoned speech directories at startup. Do not place raw recordings or scratch under artifacts. Verify `/usr/bin/ffmpeg`, `/usr/bin/ffprobe`, `bwrap`, and unprivileged user namespaces work **inside the deployed server container**. Bubblewrap's user namespace setup can be restricted by the host/container security profile; if the sandbox cannot run, leave voice disabled. Test a real 60-second supported clip within the 90-second, 2 GiB process bounds before accepting voice. The server accepts WebM/Opus or MP4/AAC recordings up to 8 MiB and 60 seconds.

## 5. Workers, checks, and operations

On each separate development machine, install the worker prerequisites and project policy from the [main README](../../README.md): Git, Bash, Bubblewrap, durable worker state, a worker-owned local repository, and the reviewer executable outside that repository. Start the existing `teem-worker --url https://teem.example.com --token ... --worker-id worker-1 --projects projects.json --state-dir ...`. Use the same worker ID and token as the server command. Workers initiate outbound HTTPS connections and require protocol version 2; do not expose a worker port on TrueNAS. The reviewer Ollama bridge connects to the **worker's** fixed `127.0.0.1:11434` endpoint. GPU inference can live on another host only if the worker provides a compatible local endpoint, such as through a worker-side tunnel; this deployment does not configure that endpoint.

Before an upgrade, stop new approvals and let active work settle, then stop workers and the Teem server. Back up the PostgreSQL database with PostgreSQL-aware tooling, the complete artifacts dataset, `teem.env`, `postgres_password`, reviewer and speech configuration, the VAPID private key, and Caddy's data/configuration. Preserve worker-local state and project repositories on their respective machines. Take the database and artifacts backup while Teem is stopped so paths recorded in the database match files on disk. A live filesystem copy of PostgreSQL data alone is not a database backup. Build a new Teem image from the intended revision under a new tag, retain the previous image, review any SQL change, update the app image tag, start PostgreSQL and Teem, then Caddy and workers. Do not rerun `schema.sql` on an existing database or apply the slice-3 SQL twice. To restore, restore the matching database and artifact set together, restore the same VAPID key and secrets, ensure dataset ACLs, and only then restart Teem and workers. Restoring a database dump already containing schema should use no initialization SQL. Restore Caddy data to retain certificate state or allow Caddy to reissue certificates.

Verify PostgreSQL reports ready and Teem has started without configuration/hash errors. `curl -i https://teem.example.com/` should give a Basic challenge; `curl -i -u 'operator:YOUR_PASSWORD' https://teem.example.com/` should return the phone page. Check `https://teem.example.com/sw.js` and `/manifest.webmanifest` at the origin root with no-store responses. There is no `/health` endpoint and the server does not implement HEAD, so use GET. Register a project as in the main README, connect an outbound worker, and complete one authorized Task through review; check that artifacts persist across a Teem restart. Verify Web Push from a real subscription and confirm the sender can reach its allowed destinations. If voice is enabled, record and transcribe a supported clip from each target phone.

For phone acceptance, record browser and OS versions on one installed iPhone Home Screen app and one installed Android PWA. On both, check installation and relaunch; Basic authentication; authenticated upload and the actual recording format; microphone track shutdown on interruption; push while backgrounded and closed; notification navigation to the current run; and ordinary Approve/Deny. On iPhone, request push permission from a direct gesture in the installed Home Screen app. Record unsupported features as fallbacks rather than passing checks. The main README has the current acceptance details.
