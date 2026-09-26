-- A project is a lowercase GitHub owner/name. Contracts carry each Run's base and checks.
CREATE TABLE IF NOT EXISTS projects (
    id text PRIMARY KEY,
    name text NOT NULL,
    grant_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    status text NOT NULL CHECK (status IN ('proposed','granted','denied','revoked')),
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS requests (
    id uuid PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    text text NOT NULL,
    classification text NOT NULL,
    context_ref text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES requests(id),
    project_id text NOT NULL REFERENCES projects(id),
    status text NOT NULL,
    contract_version integer NOT NULL DEFAULT 1,
    current_candidate_id uuid,
    stop_reason text,
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    pr_url text,
    publish_attempts integer NOT NULL DEFAULT 0,
    next_publish_at timestamptz,
    pr_state text CHECK (pr_state IN ('open','merged','closed')),
    pr_closed_at timestamptz,
    pr_checked_at timestamptz
);
DROP INDEX IF EXISTS one_active_run_per_project;
CREATE UNIQUE INDEX one_active_run_per_project ON runs(project_id)
    WHERE status IN ('awaiting_approval', 'queued', 'coding', 'awaiting_review', 'reviewing', 'uncertain', 'cancelling');

CREATE TABLE IF NOT EXISTS contracts (
    run_id uuid NOT NULL REFERENCES runs(id),
    version integer NOT NULL,
    body jsonb NOT NULL,
    proposal jsonb NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    contract_version integer NOT NULL,
    action text NOT NULL,
    decision text NOT NULL,
    source text NOT NULL,
    target text NOT NULL,
    target_state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, contract_version, action)
);

CREATE TABLE IF NOT EXISTS tasks (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    kind text NOT NULL CHECK (kind IN ('code_and_check', 'review')),
    revision_number integer NOT NULL DEFAULT 0,
    input_candidate_id uuid,
    source_review_attempt_id uuid,
    review_context jsonb,
    status text NOT NULL,
    generation integer NOT NULL DEFAULT 0,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, revision_number, kind)
);

CREATE TABLE IF NOT EXISTS attempts (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES tasks(id),
    generation integer NOT NULL,
    worker_id text NOT NULL,
    status text NOT NULL,
    lease_until timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    usage jsonb,
    result_sha256 text,
    UNIQUE (task_id, generation)
);

CREATE TABLE IF NOT EXISTS artifacts (
    attempt_id uuid PRIMARY KEY REFERENCES attempts(id),
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL,
    path text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS candidates (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    attempt_id uuid NOT NULL UNIQUE REFERENCES attempts(id),
    contract_version integer NOT NULL,
    base_commit text NOT NULL,
    head_commit text NOT NULL,
    artifact_sha256 text NOT NULL,
    check_hash text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE runs ADD CONSTRAINT current_candidate_fk FOREIGN KEY (current_candidate_id) REFERENCES candidates(id);
ALTER TABLE tasks ADD CONSTRAINT input_candidate_fk FOREIGN KEY (input_candidate_id) REFERENCES candidates(id);
ALTER TABLE tasks ADD CONSTRAINT source_review_fk FOREIGN KEY (source_review_attempt_id) REFERENCES attempts(id);

CREATE TABLE IF NOT EXISTS reviews (
    attempt_id uuid PRIMARY KEY REFERENCES attempts(id),
    raw_output text NOT NULL,
    result jsonb,
    validation_error text,
    provenance jsonb NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('accepted', 'invalid', 'stale')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY,
    run_id uuid REFERENCES runs(id),
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

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

-- Migrations in teem/migrations applied to this database; a fresh database records them all.
CREATE TABLE IF NOT EXISTS schema_migrations (
    name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Turns from the OpenAI-compatible chat endpoint; see migrations/0002_chat_turns.sql.
CREATE TABLE IF NOT EXISTS chat_turns (
    id bigserial PRIMARY KEY,
    role text NOT NULL CHECK (role IN ('user','assistant')),
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
