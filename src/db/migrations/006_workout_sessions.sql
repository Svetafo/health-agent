-- Migration 006: workout_sessions
-- Отдельные тренировочные сессии из Apple Health.
-- Одна строка = одна тренировка.

CREATE TABLE IF NOT EXISTS workout_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT        NOT NULL,

    started_at      TIMESTAMP   NOT NULL,           -- начало тренировки (локальное время)
    ended_at        TIMESTAMP   NOT NULL,           -- конец тренировки
    workout_date    DATE        NOT NULL,           -- дата начала (для группировки)

    duration_min    INTEGER,                        -- длительность, мин
    workout_type    TEXT        NOT NULL,           -- 'strength' | 'cardio' | 'low_intensity' | 'other'
    workout_source  TEXT,                           -- оригинальный HKWorkoutActivityType из Apple Health

    active_kcal     NUMERIC(8,2),                  -- активные калории
    avg_heart_rate  INTEGER,                        -- средняя ЧСС (null если нет данных)
    max_heart_rate  INTEGER,                        -- макс ЧСС
    distance_km     NUMERIC(6,3),                  -- дистанция (null для силовых)

    source          TEXT NOT NULL DEFAULT 'apple_health_export',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, started_at)
);

CREATE INDEX IF NOT EXISTS idx_workout_user_date ON workout_sessions (user_id, workout_date);
CREATE INDEX IF NOT EXISTS idx_workout_type      ON workout_sessions (user_id, workout_type);
