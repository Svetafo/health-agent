-- Migration 004: sleep_sessions
-- One row = one night per user.
-- sleep_date = date of morning wake-up (date on which sleep ended).

CREATE TABLE IF NOT EXISTS sleep_sessions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    sleep_date      DATE        NOT NULL,    -- wake-up date (morning)

    bedtime_start   TIMESTAMP,              -- went to bed (local time, no TZ)
    bedtime_end     TIMESTAMP,              -- woke up

    total_min       INTEGER,                -- net sleep = deep + REM + core, min
    in_bed_min      INTEGER,                -- in bed (total span)
    deep_min        INTEGER,                -- deep sleep (Deep)
    rem_min         INTEGER,                -- REM
    core_min        INTEGER,                -- light sleep (Core / Unspecified / legacy Asleep)
    awake_min       INTEGER,                -- awake within sleep

    efficiency_pct  NUMERIC(5,2),           -- total_min / in_bed_min * 100

    source          TEXT NOT NULL DEFAULT 'apple_health',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, sleep_date)
);

CREATE INDEX IF NOT EXISTS idx_sleep_user_date ON sleep_sessions (user_id, sleep_date);
CREATE INDEX IF NOT EXISTS idx_sleep_date      ON sleep_sessions (sleep_date);
