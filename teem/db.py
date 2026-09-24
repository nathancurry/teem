from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row)


def initialize(dsn):
    with connect(dsn) as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


def event(conn, run_id, kind, payload, notify=False):
    row = conn.execute(
        "INSERT INTO events(run_id, kind, payload) VALUES (%s, %s, %s::jsonb) RETURNING id",
        (run_id, kind, __import__("json").dumps(payload)),
    ).fetchone()
    if notify:
        conn.execute("""INSERT INTO notification_deliveries(event_id,subscription_id,state)
                        SELECT %s,id,'pending' FROM push_subscriptions""", (row["id"],))
    return row["id"]
