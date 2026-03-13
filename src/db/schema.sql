-- Health Agent: database schema

CREATE TABLE IF NOT EXISTS raw_events (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,          -- 'telegram', 'apple_health', 'n8n', ...
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dialog_sessions (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    context     JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT REFERENCES dialog_sessions(id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT NOT NULL,
    tokens_in   INT,
    tokens_out  INT,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Apple Health data normalization before insert (performed in Python):
--   - Comma decimal separator → dot: "3,14" → 3.14  (iPhone Russian locale)
--   - Empty strings → NULL: "" → None
--   - NUMERIC instead of FLOAT — exact storage, no floating point errors
--
-- One row = one day per user (Apple Health aggregates by day).
-- On duplicate submission — upsert by (user_id, recorded_date).

CREATE TABLE IF NOT EXISTS health_metrics (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    recorded_date   DATE NOT NULL,           -- date from Shortcuts (separate field)

    -- Apple Health metrics (all nullable — field may be absent)
    hrv_ms          NUMERIC(6,1),            -- HRV, ms
    vo2max          NUMERIC(5,2),            -- VO2 max, ml/kg/min
    heart_rate      NUMERIC(5,1),            -- avg heart rate, bpm
    resting_hr      NUMERIC(5,1),            -- resting heart rate, bpm
    steps           INTEGER,                 -- steps per day
    flights         INTEGER,                 -- flights climbed
    active_kcal     NUMERIC(8,2),            -- active calories, kcal
    resting_kcal    NUMERIC(8,2),            -- resting calories (BMR), kcal
    distance_km     NUMERIC(8,3),            -- distance, km
    walking_speed   NUMERIC(5,2),            -- avg walking speed, km/h

    source          TEXT NOT NULL DEFAULT 'apple_health',
    raw_event_id    BIGINT REFERENCES raw_events(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One day per user; duplicate submission → upsert
    UNIQUE (user_id, recorded_date)
);

CREATE TABLE IF NOT EXISTS nutrition_logs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    logged_date  DATE NOT NULL,
    calories     NUMERIC(8,2),
    protein      NUMERIC(6,2),
    fat          NUMERIC(6,2),
    carbs        NUMERIC(6,2),
    meals_json   JSONB,                          -- meal details
    source       TEXT DEFAULT 'screenshot',
    raw_event_id BIGINT REFERENCES raw_events(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One day per user; duplicate submission → upsert
    UNIQUE (user_id, logged_date)
);

CREATE TABLE IF NOT EXISTS user_profile (
    user_id      TEXT PRIMARY KEY,
    profile_text TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Body metrics: smart scale + manual measurements
-- One row = one day per user.
-- On duplicate submission — upsert by (user_id, recorded_date).
CREATE TABLE IF NOT EXISTS body_metrics (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    recorded_date   DATE NOT NULL,

    -- Smart scale metrics (extracted via vision LLM)
    weight          NUMERIC(5,2),            -- weight, kg
    body_fat_pct    NUMERIC(5,2),            -- body fat, %
    muscle_kg       NUMERIC(5,2),            -- muscle mass, kg
    water_pct       NUMERIC(5,2),            -- water, %
    visceral_fat    NUMERIC(5,1),            -- visceral fat (index)
    bone_mass_kg    NUMERIC(4,2),            -- bone mass, kg
    bmr_kcal        NUMERIC(6,1),            -- BMR, kcal
    bmi             NUMERIC(4,1),            -- BMI

    -- Manual measurements (centimeters)
    arms_cm         NUMERIC(5,1),            -- arms, cm
    thighs_cm       NUMERIC(5,1),            -- thighs, cm
    neck_cm         NUMERIC(5,1),            -- neck, cm
    shin_cm         NUMERIC(5,1),            -- shin, cm
    waist_cm        NUMERIC(5,1),            -- waist, cm
    chest_cm        NUMERIC(5,1),            -- chest, cm
    hips_cm         NUMERIC(5,1),            -- hips, cm

    source          TEXT NOT NULL DEFAULT 'manual',  -- 'scale_photo' | 'text' | 'manual'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, recorded_date)
);

-- Memory patterns: accumulating evidence before user verification
-- Flow: Synthesizer detects pattern → pending (confirmations++)
--        → at confirmations >= 3: semantic filter → /memory verification
--        → confirmed: moves to memory_insights
--        → expired: not confirmed within TTL → deleted
CREATE TABLE IF NOT EXISTS pending_insights (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pattern_text    TEXT NOT NULL,              -- pattern text
    confirmations   INT NOT NULL DEFAULT 1,     -- how many times pattern was observed
    first_seen      DATE NOT NULL DEFAULT CURRENT_DATE,
    last_seen       DATE NOT NULL DEFAULT CURRENT_DATE,
    expires_at      DATE NOT NULL,              -- TTL: if not confirmed — deleted
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired'))
);

-- Confirmed patterns: agent reads on every request
CREATE TABLE IF NOT EXISTS memory_insights (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    insight_text    TEXT NOT NULL,              -- pattern confirmed by user
    confirmed_at    DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Message embeddings for semantic search (pgvector)
-- Requires: CREATE EXTENSION vector; (migration 001_pgvector.sql)
CREATE TABLE IF NOT EXISTS message_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_raw_events_source   ON raw_events(source);
CREATE INDEX IF NOT EXISTS idx_raw_events_created  ON raw_events(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session    ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_health_user_date    ON health_metrics(user_id, recorded_date);
CREATE INDEX IF NOT EXISTS idx_health_date         ON health_metrics(recorded_date);
CREATE INDEX IF NOT EXISTS idx_nutrition_user_date ON nutrition_logs(user_id, logged_date);
CREATE INDEX IF NOT EXISTS idx_body_user_date      ON body_metrics(user_id, recorded_date);
