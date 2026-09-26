import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row)


MIGRATIONS = Path(__file__).with_name("migrations")


def migrate(dsn):
    """Bring a database to the current schema and return what was applied.

    A fresh database gets schema.sql and records every migration as applied. An existing one gets
    each migration it lacks, in name order, all in one transaction.
    """
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    with connect(dsn) as conn:
        if not conn.execute("SELECT to_regclass('public.runs') AS t").fetchone()["t"]:
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
            for name in names:
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
            return ["schema.sql"]
        if not conn.execute("SELECT to_regclass('public.telegram_updates') AS t").fetchone()["t"]:
            raise RuntimeError("this database predates slice 4; apply docs/architecture/slice-4.sql by hand first")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        done = []
        for name in names:
            if name not in applied:
                conn.execute((MIGRATIONS / name).read_text())
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
                done.append(name)
        return done


def initialize(dsn):
    migrate(dsn)


def event(conn, run_id, kind, payload, notify=False):
    row = conn.execute(
        "INSERT INTO events(run_id, kind, payload) VALUES (%s, %s, %s::jsonb) RETURNING id",
        (run_id, kind, json.dumps(payload)),
    ).fetchone()
    if notify:
        # The sender composes the text from current Run state when it sends.
        conn.execute("INSERT INTO telegram_outbox(run_id,state) VALUES (%s,'pending')", (run_id,))
    return row["id"]
