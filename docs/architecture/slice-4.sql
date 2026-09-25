-- Apply once to an existing slice-3 database before starting a slice-4 server.
-- Slice-3 projects were local registrations. They keep their Run history but are
-- marked revoked; new work names a GitHub owner/name project.
ALTER TABLE projects ADD COLUMN grant_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid();
ALTER TABLE projects ADD COLUMN status text NOT NULL DEFAULT 'revoked'
    CHECK (status IN ('proposed','granted','denied','revoked'));
ALTER TABLE projects ALTER COLUMN status DROP DEFAULT;
ALTER TABLE projects ADD COLUMN decided_at timestamptz;
ALTER TABLE projects DROP COLUMN base_commit, DROP COLUMN checks, DROP COLUMN check_hash;

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id bigint PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('text','voice','callback','unsupported','ignored')),
    payload jsonb NOT NULL,
    text text,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id bigserial PRIMARY KEY,
    run_id uuid REFERENCES runs(id),
    project_id text REFERENCES projects(id),
    text text,
    state text NOT NULL CHECK (state IN ('pending','sent','abandoned')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 6),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    telegram_message_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (text IS NOT NULL OR run_id IS NOT NULL OR project_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS pending_telegram_outbox
    ON telegram_outbox(next_attempt_at) WHERE state='pending';
