-- Conversation turns from the OpenAI-compatible chat endpoint (voice apps), kept so the decider
-- sees one conversation across Telegram and voice.
CREATE TABLE IF NOT EXISTS chat_turns (
    id bigserial PRIMARY KEY,
    role text NOT NULL CHECK (role IN ('user','assistant')),
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
