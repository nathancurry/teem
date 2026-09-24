CREATE TABLE IF NOT EXISTS projects (
    id text PRIMARY KEY,
    name text NOT NULL,
    base_commit text NOT NULL CHECK (base_commit ~ '^[0-9a-f]{40}$'),
    checks jsonb NOT NULL,
    check_hash text NOT NULL,
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
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
DROP INDEX IF EXISTS one_active_run_per_project;
CREATE UNIQUE INDEX one_active_run_per_project ON runs(project_id)
    WHERE status IN ('awaiting_approval', 'queued', 'coding', 'uncertain', 'cancelling');

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
    kind text NOT NULL CHECK (kind = 'code_and_check'),
    status text NOT NULL,
    generation integer NOT NULL DEFAULT 0,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
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

CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY,
    run_id uuid REFERENCES runs(id),
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
