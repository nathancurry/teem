-- Apply once to a v0.0.7 database (after slice-4.sql) before starting a v0.0.8 server.
-- Records what happened to each Run's pull request after Teem opened it.
ALTER TABLE runs ADD COLUMN pr_state text CHECK (pr_state IN ('open','merged','closed')),
    ADD COLUMN pr_closed_at timestamptz, ADD COLUMN pr_checked_at timestamptz;
