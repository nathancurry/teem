# TrueNAS SCALE deployment

This is a single-host example for a Docker-based TrueNAS SCALE release with **Install via YAML**. Replace `storage`, `teem.example.com`, and all example credentials before pasting the two Compose files into separate apps. The Teem server serves the PWA files from its installed package and runs the push sender in the same process; neither needs another container. Workers and their repositories stay on other machines. No GPU or inference service is part of these YAML files. TrueNAS documents [Install via YAML](https://apps.truenas.com/managing-apps/installing-custom-apps/) as a Docker Compose editor.

## 1. Host storage and access

Create these paths before installing either app. Use your pool name in place of `storage` in the paths below and both Compose files.

| Host path | Container path | Access and owner |
| --- | --- | --- |
| `/mnt/storage/teem/postgres` | `/var/lib/postgresql/data` | PostgreSQL image's `postgres` user, private read/write; empty on first boot |
| `/mnt/storage/teem/artifacts` | `/srv/teem/artifacts` | UID/GID 568, private read/write |
| `/mnt/storage/teem/config` | `/srv/teem/config` | UID 568 can read; only an administrator can change files |
| `/mnt/storage/teem/speech` (voice only) | `/srv/teem/speech` | UID 568 can read; mount read-only |
| `/mnt/storage/teem/caddy-config/Caddyfile` | `/etc/caddy/Caddyfile` | Readable by Caddy |
| `/mnt/storage/teem/caddy-data` | `/data` | Caddy can write; stores ACME certificates |

Use private dataset ACLs, not a public share. The PostgreSQL image manages ownership of an empty data directory; confirm its runtime user can write it before starting. Set the artifact dataset's owner to 568:568. Make `teem.env`, `postgres_password`, `vapid.pem`, and `reviewer.json` readable only by their required container user and administrators. A source dataset mounted read-only in the container does not replace host-side ACLs. Keep the PostgreSQL data and artifacts on durable storage with enough free space for 50 MiB candidate bundles and normal PostgreSQL growth.

Put the current [`teem/schema.sql`](../../teem/schema.sql) at `/mnt/storage/teem/config/schema.sql` and copy [`env.example`](env.example) to `/mnt/storage/teem/config/teem.env`. Create `/mnt/storage/teem/config/postgres_password` as a single-line file containing the database password. Put the same password, URL-encoded where needed, into `TEEM_DSN`. The Compose file uses Docker's `POSTGRES_PASSWORD_FILE`; Teem itself receives the DSN as its existing `--dsn` option. `TEEM_*` names in `teem.env` are used by the Compose shell wrapper, not read by Teem as application settings. Keep the env file out of source control. Docker process arguments and container metadata can expose CLI credentials to TrueNAS administrators; restrict host and app management access accordingly.

Copy [`reviewer.example.json`](reviewer.example.json) to `/mnt/storage/teem/config/reviewer.json`. Its identity, instructions hash, destination, model, and timeout must match the worker policy in the [main README](../../README.md). After building or pulling the image below, generate one VAPID private key, then keep that exact key through upgrades and restore it with the secrets:

```sh
docker run --rm --entrypoint python -v /mnt/storage/teem/config:/srv/teem/config ghcr.io/nathancurry/teem:v0.0.1 \
  -c 'from py_vapid import Vapid02; v=Vapid02(); v.generate_keys(); v.save_key("/srv/teem/config/vapid.pem")'
chmod 600 /mnt/storage/teem/config/vapid.pem
```

Copy [`telegram.example.json`](telegram.example.json) to `/mnt/storage/teem/config/telegram.json`. Put in the bot token from @BotFather and your numeric Telegram user ID, and make it readable only by UID 568 and administrators. The server long-polls `api.telegram.org` over outbound HTTPS; no webhook or inbound port is needed. It accepts messages only from that user's private chat. Send the bot one message after deploying so the chat exists. To run without Telegram, remove `--telegram-config` from the command. Copy [`github.example.json`](github.example.json) to `github.json` with the GitHub owners Teem may work under; the token is needed only for private repositories. Copy [`decider.example.json`](decider.example.json) to `decider.json` with an OpenRouter key and model slug. Restrict both to UID 568 and administrators. The server also needs outbound HTTPS to `github.com` and `openrouter.ai`, and keeps rebuildable repository mirrors under the artifacts dataset in `mirrors/`.

Set `TEEM_VAPID_SUBJECT` to a real `mailto:` contact. The push sender starts automatically when both VAPID options are passed; there is no separate push command. Its outbound HTTPS/DNS access must reach `fcm.googleapis.com`, `updates.push.services.mozilla.com`, and `*.push.apple.com`.

## 2. Teem image and database

Build the repository's root [`Dockerfile`](../../Dockerfile) on the TrueNAS host from the checked-out revision. Its `server` target installs Teem and the PWA from `pyproject.toml` and `uv.lock`, plus Git for server-side candidate inspection. It runs as UID/GID 568. The default image has no FFmpeg, Bubblewrap, whisper executable, or model. From the repository root, build the tag used in `teem.compose.yaml`:

```sh
docker build --target server -t ghcr.io/nathancurry/teem:v0.0.1 .
docker run --rm ghcr.io/nathancurry/teem:v0.0.1 serve --help
```

The image contains no credentials or deployment data; the Compose file bind-mounts artifacts and read-only configuration.

GitHub Actions publishes both targets for every `v*` tag: `server` as `ghcr.io/nathancurry/teem:vX.Y.Z` and `speech` as `ghcr.io/nathancurry/teem:vX.Y.Z-speech`. There is no `latest` tag. The TrueNAS deployment pins an explicit version tag and uses `pull_policy: always` so the tagged image is refreshed when the app is deployed or updated. Configure GHCR credentials on TrueNAS only if the package is private.

The Compose example uses teem-server serve and binds its internal listener to 0.0.0.0 only inside the container. Docker publishes port 8765 on the TrueNAS host's 127.0.0.1 only; PostgreSQL has no published port. Keep the server to one replica because the current implementation embeds its sweep and push loops in that process.

The official PostgreSQL image creates database/user `teem` on an **empty** data directory and runs the mounted `schema.sql` once through `/docker-entrypoint-initdb.d`. The server waits for `pg_isready`, which checks database availability but is not an application health endpoint. The current `teem-server init --dsn ...` executes the same [`teem/schema.sql`](../../teem/schema.sql); run it **only for a fresh database** when not using the first-boot mount. It is not a migration tool, and `schema.sql` includes non-idempotent constraint additions. Existing slice-1 databases require manual inspection and SQL alteration. For an existing slice-2 database, apply [`docs/architecture/slice-3.sql`](../../docs/architecture/slice-3.sql) exactly once with a PostgreSQL client before starting this server. That SQL adds push subscriptions and delivery rows; it does not backfill old events. For an existing slice-3 database, apply [`docs/architecture/slice-4.sql`](../../docs/architecture/slice-4.sql) exactly once; it adds the Telegram update log and outbox, and marks slice-3 local projects revoked because projects are now GitHub repositories. A restored database already containing the current schema must not run either SQL again.

## 3. HTTPS and startup

Copy [`Caddyfile.example`](Caddyfile.example) to `/mnt/storage/teem/caddy-config/Caddyfile` and replace its hostname. Set `TEEM_ORIGIN` to exactly `https://` plus that hostname, without a trailing slash. Give the hostname working DNS and certificate issuance reachability. Caddy's host networking needs host TCP ports 80 and 443; move the TrueNAS management UI off those ports first and restrict its new port separately. Install `teem.compose.yaml` as one app, confirm PostgreSQL initialization and Teem startup, then install `caddy.compose.yaml` as a second app. The host-network Caddy container connects to the loopback-only published Teem port. Caddy obtains and renews certificates and persists them under `caddy-data`. See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) for DNS and challenge requirements.

The Caddyfile proxies the whole origin, including `/sw.js` at the root so it can control the PWA. It preserves incoming `Authorization` and `Origin` headers; Teem requires Basic authentication and same-origin browser POSTs, while workers use a bearer token. Teem already returns `Cache-Control: no-store` on its responses, including static assets. Caddy limits `/transcribe` requests to 8 MiB and allows 210 seconds to read the upstream response, covering Teem's 180-second inference bound. Do not put a site-wide 8 MiB limit in front of `/worker/upload/`, which accepts candidate bundles up to 50 MiB. The browser has a 30-second upload timeout. [Caddy's `request_body` directive](https://caddyserver.com/docs/caddyfile/directives/request_body) requires Caddy 2.10 or later.

## 4. Optional local dictation

The example starts with voice disabled: it passes neither speech option. `/transcribe` then returns 503 after authentication. For voice, change the server image in [`teem.compose.yaml`](teem.compose.yaml) from `ghcr.io/nathancurry/teem:v0.0.1` to `ghcr.io/nathancurry/teem:v0.0.1-speech` (or the matching `${tag}-speech` for your release). That image adds FFmpeg/ffprobe and Bubblewrap. To build it locally instead of pulling the published image:

```sh
docker build --target speech -t ghcr.io/nathancurry/teem:v0.0.1-speech .
```

Install a static `whisper-cli` built from whisper.cpp v1.9.4 and the `ggml-base.en.bin` model in `/mnt/storage/teem/speech/`, readable by UID 568. Neither is baked into either image. Copy [`speech.example.json`](speech.example.json) to `/mnt/storage/teem/config/speech.json`. Calculate SHA-256 for the exact files installed on TrueNAS and replace both all-zero placeholders in `speech.json` with the resulting 64-character hex digests:

```sh
sha256sum /mnt/storage/teem/speech/whisper-cli /mnt/storage/teem/speech/ggml-base.en.bin
```

In `teem.compose.yaml`, uncomment the read-only `/mnt/storage/teem/speech:/srv/teem/speech:ro` mount and the private, UID 568 owned tmpfs at `/srv/teem/speech-scratch`. Add `--speech-config /srv/teem/config/speech.json --speech-scratch /srv/teem/speech-scratch` to the server command and redeploy. The scratch tmpfs keeps recordings outside artifacts and backups; Teem clears abandoned speech directories at startup. Verify `/usr/bin/ffmpeg`, `/usr/bin/ffprobe`, `bwrap`, and unprivileged user namespaces work **inside the deployed server container**. Bubblewrap's user namespace setup can be restricted by the host/container security profile; if the sandbox cannot run, leave voice disabled. Test a real 120-second supported clip within the 180-second, 2 GiB process bounds before accepting voice; `ggml-base.en.bin` is fast but inaccurate, so measure a larger English model too. The server accepts WebM/Opus, MP4/AAC, and Telegram Ogg/Opus recordings up to 8 MiB and 120 seconds.

## 5. Workers, checks, and operations

On the separate development machine, follow [worker setup](../worker/README.md) for rootless Podman, the agent image, the internal network, and the allowlisting proxy, then write the worker policy described in the [main README](../../README.md). Start `teem-worker --url https://teem.example.com --token ... --worker-id worker-1 --projects policy.json --state-dir ...`. Use the same worker ID and token as the server command. Workers initiate outbound HTTPS connections and require protocol version 3; do not expose a worker port on TrueNAS.

Before an upgrade, stop new approvals and let active work settle, then stop workers and the Teem server. Back up the PostgreSQL database with PostgreSQL-aware tooling, the complete artifacts dataset, `teem.env`, `postgres_password`, reviewer and speech configuration, the VAPID private key, and Caddy's data/configuration. Preserve worker-local state and project repositories on their respective machines. Take the database and artifacts backup while Teem is stopped so paths recorded in the database match files on disk. A live filesystem copy of PostgreSQL data alone is not a database backup. Build a new Teem image from the intended revision under a new tag, retain the previous image, review any SQL change, update the app image tag, start PostgreSQL and Teem, then Caddy and workers. Do not rerun `schema.sql` on an existing database or apply the slice-3 SQL twice. To restore, restore the matching database and artifact set together, restore the same VAPID key and secrets, ensure dataset ACLs, and only then restart Teem and workers. Restoring a database dump already containing schema should use no initialization SQL. Restore Caddy data to retain certificate state or allow Caddy to reissue certificates.

Verify PostgreSQL reports ready and Teem has started without configuration/hash errors. `curl -i https://teem.example.com/` should give a Basic challenge; `curl -i -u 'operator:YOUR_PASSWORD' https://teem.example.com/` should return the phone page. Check `https://teem.example.com/sw.js` and `/manifest.webmanifest` at the origin root with no-store responses. There is no `/health` endpoint and the server does not implement HEAD, so use GET. Register a project as in the main README, connect an outbound worker, and complete one authorized Task through review; check that artifacts persist across a Teem restart. Verify Web Push from a real subscription and confirm the sender can reach its allowed destinations. If voice is enabled, record and transcribe a supported clip from each target phone.

For phone acceptance, record browser and OS versions on one installed iPhone Home Screen app and one installed Android PWA. On both, check installation and relaunch; Basic authentication; authenticated upload and the actual recording format; microphone track shutdown on interruption; push while backgrounded and closed; notification navigation to the current run; and ordinary Approve/Deny. On iPhone, request push permission from a direct gesture in the installed Home Screen app. Record unsupported features as fallbacks rather than passing checks. The main README has the current acceptance details.
