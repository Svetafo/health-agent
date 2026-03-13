-- Migration 006: workout_sessions
-- Individual workout sessions from Apple Health.
-- One row = one workout.

CREATE TABLE IF NOT EXISTS workout_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT        NOT NULL,

    started_at      TIMESTAMP   NOT NULL,           -- workout start (local time)
    ended_at        TIMESTAMP   NOT NULL,           -- workout end
    workout_date    DATE        NOT NULL,           -- start date (for grouping)

    duration_min    INTEGER,                        -- duration, min
    workout_type    TEXT        NOT NULL,           -- 'strength' | 'cardio' | 'low_intensity' | 'other'
    workout_source  TEXT,                           -- original HKWorkoutActivityType from Apple Health

    active_kcal     NUMERIC(8,2),                  -- active calories
    avg_heart_rate  INTEGER,                        -- avg heart rate (null if no data)
    max_heart_rate  INTEGER,                        -- max heart rate
    distance_km     NUMERIC(6,3),                  -- distance (null for strength)

    source          TEXT NOT NULL DEFAULT 'apple_health_export',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, started_at)
);

CREATE INDEX IF NOT EXISTS idx_workout_user_date ON workout_sessions (user_id, workout_date);
CREATE INDEX IF NOT EXISTS idx_workout_type      ON workout_sessions (user_id, workout_type);
