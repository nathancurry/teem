# TrueNAS SCALE deployment

This is a single-host example for a Docker-based TrueNAS SCALE release with **Install via YAML**. Replace `storage`, `teem.example.com`, and all example credentials before pasting the two Compose files into separate apps. The Teem server runs the Telegram bot, decider, and GitHub publisher in the same process; none needs another container. Workers and their repositories stay on other machines. No GPU or inference service is part of these YAML files. TrueNAS documents [Install via YAML](https://apps.truenas.com/managing-apps/installing-custom-apps/) as a Docker Compose editor.

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

Use private dataset ACLs, not a public share. The PostgreSQL image manages ownership of an empty data directory; confirm its runtime user can write it before starting. Set the artifact dataset's owner to 568:568. Make `teem.env`, `postgres_password`, and the JSON configuration files readable only by their required container user and administrators. A source dataset mounted read-only in the container does not replace host-side ACLs. Keep the PostgreSQL data and artifacts on durable storage with enough free space for 50 MiB candidate bundles and normal PostgreSQL growth.

Put the current [`teem/schema.sql`](../../teem/schema.sql) at `/mnt/storage/teem/config/schema.sql` and copy [`env.example`](env.example) to `/mnt/storage/teem/config/teem.env`. Create `/mnt/storage/teem/config/postgres_password` as a single-line file containing the database password. Put the same password, URL-encoded where needed, into `TEEM_DSN`. The Compose file uses Docker's `POSTGRES_PASSWORD_FILE`; Teem itself receives the DSN as its existing `--dsn` option. `TEEM_*` names in `teem.env` are used by the Compose shell wrapper, not read by Teem as application settings. Keep the env file out of source control. Docker process arguments and container metadata can expose CLI credentials to TrueNAS administrators; restrict host and app management access accordingly.

Copy [`reviewer.example.json`](reviewer.example.json) to `/mnt/storage/teem/config/reviewer.json`. Its identity, destination, and timeout, and the SHA-256 of its instructions, must match the worker policy in the [main README](../../README.md).

Copy [`telegram.example.json`](telegram.example.json) to `/mnt/storage/teem/config/telegram.json`. Put in the bot token from @BotFather and your numeric Telegram user ID, and make it readable only by UID 568 and administrators. The server long-polls `api.telegram.org` over outbound HTTPS; no webhook or inbound port is needed. It accepts messages only from that user's private chat. Send the bot one message after deploying so the chat exists. Copy [`github.example.json`](github.example.json) to `github.json`. List the GitHub owners Teem may work under, with a fine-grained token that has read/write access to contents and pull requests on their repositories. The server pushes only `teem/*` branches and opens pull requests; it never merges. Copy [`decider.example.json`](decider.example.json) to `decider.json` with an OpenRouter key and model slug. Restrict both to UID 568 and administrators. The server needs outbound HTTPS to `api.telegram.org`, `openrouter.ai`, `github.com`, and `api.github.com`. It keeps rebuildable repository mirrors under the artifacts dataset in `mirrors/`.

## 2. Teem image and database

Build the repository's root [`Dockerfile`](../../Dockerfile) on the TrueNAS host from the checked-out revision. Its `server` target installs Teem and its evidence pages from `pyproject.toml` and `uv.lock`, plus Git for server-side candidate inspection. It runs as UID/GID 568. The default image has no FFmpeg, Bubblewrap, whisper executable, or model. From the repository root, build the tag used in `teem.compose.yaml`:

```sh
docker build --target server -t ghcr.io/nathancurry/teem:v0.0.1 .
docker run --rm ghcr.io/nathancurry/teem:v0.0.1 serve --help
```

The image contains no credentials or deployment data; the Compose file bind-mounts artifacts and read-only configuration.

GitHub Actions publishes both targets for every `v*` tag: `server` as `ghcr.io/nathancurry/teem:vX.Y.Z` and `speech` as `ghcr.io/nathancurry/teem:vX.Y.Z-speech`. There is no `latest` tag. The TrueNAS deployment pins an explicit version tag and uses `pull_policy: always` so the tagged image is refreshed when the app is deployed or updated. Configure GHCR credentials on TrueNAS only if the package is private.

The Compose example uses teem-server serve and binds its internal listener to 0.0.0.0 only inside the container. Docker publishes port 8765 on the TrueNAS host's 127.0.0.1 only; PostgreSQL has no published port. Keep the server to one replica because the current implementation embeds its sweep, Telegram, and publisher loops in that process.

The official PostgreSQL image creates database/user `teem` on an **empty** data directory and runs the mounted `schema.sql` once through `/docker-entrypoint-initdb.d`. The server waits for `pg_isready`, which checks database availability but is not an application health endpoint. The current `teem-server init --dsn ...` executes the same [`teem/schema.sql`](../../teem/schema.sql); run it **only for a fresh database** when not using the first-boot mount. It is not a migration tool, and `schema.sql` includes non-idempotent constraint additions. Existing slice-1 databases require manual inspection and SQL alteration. For an existing slice-2 database, apply [`docs/architecture/slice-3.sql`](../../docs/architecture/slice-3.sql) exactly once with a PostgreSQL client before starting this server. That SQL adds push subscriptions and delivery rows; it does not backfill old events. For an existing slice-3 database, apply [`docs/architecture/slice-4.sql`](../../docs/architecture/slice-4.sql) exactly once; it adds the Telegram update log and outbox, and marks slice-3 local projects revoked because projects are now GitHub repositories. A restored database already containing the current schema must not run either SQL again.

## 3. HTTPS and startup

Copy [`Caddyfile.example`](Caddyfile.example) to `/mnt/storage/teem/caddy-config/Caddyfile` and replace its hostname. Set `TEEM_ORIGIN` to exactly `https://` plus that hostname, without a trailing slash. Give the hostname working DNS and certificate issuance reachability. Caddy's host networking needs host TCP ports 80 and 443; move the TrueNAS management UI off those ports first and restrict its new port separately. Install `teem.compose.yaml` as one app, confirm PostgreSQL initialization and Teem startup, then install `caddy.compose.yaml` as a second app. The host-network Caddy container connects to the loopback-only published Teem port. Caddy obtains and renews certificates and persists them under `caddy-data`. See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) for DNS and challenge requirements.

The Caddyfile proxies the whole origin. It preserves incoming `Authorization` and `Origin` headers. The evidence pages require Basic authentication and same-origin browser POSTs, while workers use a bearer token. Teem returns `Cache-Control: no-store` on its responses. Do not put a small request-size limit in front of `/worker/upload/`, which accepts candidate bundles up to 50 MiB. Telegram is reached outbound, so it needs no proxy route.

## 4. Optional voice notes

The example starts with voice disabled: it passes neither speech option, and the bot asks you to type instead. For voice, change the server image in [`teem.compose.yaml`](teem.compose.yaml) from `ghcr.io/nathancurry/teem:v0.0.1` to `ghcr.io/nathancurry/teem:v0.0.1-speech` (or the matching `${tag}-speech` for your release). That image adds FFmpeg/ffprobe and Bubblewrap. To build it locally instead of pulling the published image:

```sh
docker build --target speech -t ghcr.io/nathancurry/teem:v0.0.1-speech .
```

Install a static `whisper-cli` built from whisper.cpp v1.9.4 and the `ggml-base.en.bin` model in `/mnt/storage/teem/speech/`, readable by UID 568. Neither is baked into either image. Copy [`speech.example.json`](speech.example.json) to `/mnt/storage/teem/config/speech.json`. Calculate SHA-256 for the exact files installed on TrueNAS and replace both all-zero placeholders in `speech.json` with the resulting 64-character hex digests:

```sh
sha256sum /mnt/storage/teem/speech/whisper-cli /mnt/storage/teem/speech/ggml-base.en.bin
```

In `teem.compose.yaml`, uncomment the read-only `/mnt/storage/teem/speech:/srv/teem/speech:ro` mount and the private, UID 568 owned tmpfs at `/srv/teem/speech-scratch`. Add `--speech-config /srv/teem/config/speech.json --speech-scratch /srv/teem/speech-scratch` to the server command and redeploy. The scratch tmpfs keeps recordings outside artifacts and backups; Teem clears abandoned speech directories at startup. Verify `/usr/bin/ffmpeg`, `/usr/bin/ffprobe`, `bwrap`, and unprivileged user namespaces work **inside the deployed server container**. Bubblewrap's user namespace setup can be restricted by the host/container security profile; if the sandbox cannot run, leave voice disabled. Test a real 120-second supported clip within the 180-second, 2 GiB process bounds before accepting voice; `ggml-base.en.bin` is fast but inaccurate, so measure a larger English model too. The server transcribes Telegram Ogg/Opus voice notes up to 8 MiB and 120 seconds.

## 5. Workers, checks, and operations

On the separate development machine, follow [worker setup](../worker/README.md) for rootless Podman, the agent image, the internal network, and the allowlisting proxy, then write the worker policy described in the [main README](../../README.md). Start `teem-worker --url https://teem.example.com --token ... --worker-id worker-1 --projects policy.json --state-dir ...`. Use the same worker ID and token as the server command. Workers initiate outbound HTTPS connections and require protocol version 3; do not expose a worker port on TrueNAS.

Before an upgrade, stop new approvals and let active work settle, then stop workers and the Teem server. Back up the PostgreSQL database with PostgreSQL-aware tooling, the complete artifacts dataset, `teem.env`, `postgres_password`, the reviewer, speech, GitHub, decider, and Telegram configuration, and Caddy's data/configuration. Preserve worker-local state and project repositories on their respective machines. Take the database and artifacts backup while Teem is stopped so paths recorded in the database match files on disk. A live filesystem copy of PostgreSQL data alone is not a database backup. Build a new Teem image from the intended revision under a new tag, retain the previous image, review any SQL change, update the app image tag, start PostgreSQL and Teem, then Caddy and workers. Do not rerun `schema.sql` on an existing database or apply migration SQL twice. To restore, restore the matching database and artifact set together, restore the same secrets, ensure dataset ACLs, and only then restart Teem and workers. Restoring a database dump already containing schema should use no initialization SQL. Restore Caddy data to retain certificate state or allow Caddy to reissue certificates.

Verify PostgreSQL reports ready and Teem has started without configuration or hash errors. `curl -i https://teem.example.com/` should give a Basic challenge, and `curl -i -u 'operator:YOUR_PASSWORD' https://teem.example.com/` should return the Runs page. There is no `/health` endpoint and the server does not implement HEAD, so use GET. Then run the [slice-4 manual acceptance](../../docs/architecture/vertical-slice-4.md#acceptance): message the bot, have it propose work on a scratch repository, approve it, and confirm that a pull request opens and the check-in arrives on your phone.
