-- v0.0.8: what happened to each Run's pull request after Teem opened it.
-- IF NOT EXISTS: some databases had this applied by hand before migrations were tracked.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS pr_state text CHECK (pr_state IN ('open','merged','closed')),
    ADD COLUMN IF NOT EXISTS pr_closed_at timestamptz, ADD COLUMN IF NOT EXISTS pr_checked_at timestamptz;
