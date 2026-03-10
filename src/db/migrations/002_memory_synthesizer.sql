-- Memory Synthesizer tables

CREATE TABLE IF NOT EXISTS pending_insights (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    pattern_text TEXT        NOT NULL,
    confirmations INT        NOT NULL DEFAULT 1,
    first_seen   DATE        NOT NULL DEFAULT CURRENT_DATE,
    last_seen    DATE        NOT NULL DEFAULT CURRENT_DATE,
    expires_at   DATE        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending'  -- 'pending' | 'confirmed' | 'rejected'
);

CREATE INDEX IF NOT EXISTS idx_pending_insights_user_status
    ON pending_insights (user_id, status);

CREATE TABLE IF NOT EXISTS memory_insights (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    insight_text TEXT        NOT NULL,
    confirmed_at DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_insights_user
    ON memory_insights (user_id);
