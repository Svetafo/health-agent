-- Migration 004: sleep_sessions
-- Одна строка = одна ночь на пользователя.
-- sleep_date = дата утреннего пробуждения (дата, на которую завершился сон).

CREATE TABLE IF NOT EXISTS sleep_sessions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    sleep_date      DATE        NOT NULL,    -- дата пробуждения (утро)

    bedtime_start   TIMESTAMP,              -- лёг (локальное время без TZ)
    bedtime_end     TIMESTAMP,              -- встал

    total_min       INTEGER,                -- чистый сон = deep + REM + core, мин
    in_bed_min      INTEGER,                -- в постели (total span)
    deep_min        INTEGER,                -- глубокий (Deep)
    rem_min         INTEGER,                -- REM
    core_min        INTEGER,                -- лёгкий (Core / Unspecified / legacy Asleep)
    awake_min       INTEGER,                -- бодрствование внутри сна

    efficiency_pct  NUMERIC(5,2),           -- total_min / in_bed_min * 100

    source          TEXT NOT NULL DEFAULT 'apple_health',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, sleep_date)
);

CREATE INDEX IF NOT EXISTS idx_sleep_user_date ON sleep_sessions (user_id, sleep_date);
CREATE INDEX IF NOT EXISTS idx_sleep_date      ON sleep_sessions (sleep_date);
