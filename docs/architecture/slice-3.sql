-- Apply once to an existing slice-2 database before starting the slice-3 server.
CREATE TABLE push_subscriptions (
    id uuid PRIMARY KEY,
    endpoint text NOT NULL UNIQUE,
    p256dh text NOT NULL,
    auth text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notification_deliveries (
    event_id bigint NOT NULL REFERENCES events(id),
    subscription_id uuid NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    state text NOT NULL CHECK (state IN ('pending','accepted','abandoned')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 6),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (event_id, subscription_id)
);
CREATE INDEX pending_notification_deliveries
    ON notification_deliveries(next_attempt_at) WHERE state='pending';
