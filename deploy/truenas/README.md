# TrueNAS deployment

[`install/nas.sh`](../../install/nas.sh) deploys and updates Teem on TrueNAS SCALE from a clone of this repository; the [main README](../../README.md#server-setup-truenas) describes it. This directory holds the example configuration files it copies into `/mnt/storage/teem/config`.

## Switching from the "Install via YAML" app

Earlier versions ran Teem as a TrueNAS app pasted from YAML, with images from GitHub. The script uses the same data (`/mnt/storage/teem/postgres`, `artifacts`, and `config`), so switching over keeps everything:

1. Stop the old app from starting any more work, then back up the database from its PostgreSQL container:
   ```sh
   docker exec <old-postgres-container> pg_dump -U teem -d teem -Fc > /root/teem-before-switch.dump
   ```
2. In Apps, stop the Teem app and delete it. Deleting the app leaves the host paths under `/mnt/storage/teem` alone.
3. Clone and run the script:
   ```sh
   git clone https://github.com/nathancurry/teem.git /mnt/storage/teem/repo
   git -C /mnt/storage/teem/repo checkout v0.0.8
   sudo /mnt/storage/teem/repo/install/nas.sh
   ```
   It refuses to start while any other container still uses port 8765 or the PostgreSQL data directory. It keeps your existing `teem.env` values and `postgres_password`, and applies the `v0.0.8` migration.
4. `tailscale serve` still points at `127.0.0.1:8765`, so nothing changes there. The old `schema.sql` file in `config/` is no longer used and can be deleted.

## Access

The server listens only on `127.0.0.1:8765`. Publish it to your tailnet with `tailscale serve` (for example `--https=8443`), and set `TEEM_ORIGIN` in `teem.env` to that exact address. No port needs to be opened: Telegram, OpenRouter, and GitHub are all reached outbound.
