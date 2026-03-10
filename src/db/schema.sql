-- BMindset: основные таблицы

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

-- Нормализация данных Apple Health перед вставкой (выполняется в Python):
--   - Числа с запятой → точка: "3,14" → 3.14  (русская локаль iPhone)
--   - Пустые строки → NULL: "" → None
--   - Используется NUMERIC вместо FLOAT — точное хранение, без floating point ошибок
--
-- Одна строка = один день на пользователя (Apple Health агрегирует по суткам).
-- При повторной отправке — upsert по (user_id, recorded_date).

CREATE TABLE IF NOT EXISTS health_metrics (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    recorded_date   DATE NOT NULL,           -- дата из Shortcuts (отдельное поле)

    -- Метрики из Apple Health (все nullable — поле может отсутствовать)
    hrv_ms          NUMERIC(6,1),            -- вариабельность ЧСС, мс
    vo2max          NUMERIC(5,2),            -- VO2 max, мл/кг/мин
    heart_rate      NUMERIC(5,1),            -- ЧСС средняя, уд/мин
    resting_hr      NUMERIC(5,1),            -- ЧСС в покое, уд/мин
    steps           INTEGER,                 -- шаги за день
    flights         INTEGER,                 -- пролёты лестниц
    active_kcal     NUMERIC(8,2),            -- активные калории, ккал
    resting_kcal    NUMERIC(8,2),            -- калории покоя (BMR), ккал
    distance_km     NUMERIC(8,3),            -- дистанция, км
    walking_speed   NUMERIC(5,2),            -- средняя скорость ходьбы, км/ч

    source          TEXT NOT NULL DEFAULT 'apple_health',
    raw_event_id    BIGINT REFERENCES raw_events(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Один день на пользователя; повторная отправка → upsert
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
    meals_json   JSONB,                          -- детали по приёмам пищи
    source       TEXT DEFAULT 'screenshot',
    raw_event_id BIGINT REFERENCES raw_events(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Один день на пользователя; повторная отправка → upsert
    UNIQUE (user_id, logged_date)
);

CREATE TABLE IF NOT EXISTS user_profile (
    user_id      TEXT PRIMARY KEY,
    profile_text TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Метрики тела: умные весы + ручные замеры
-- Одна строка = один день на пользователя.
-- При повторной отправке — upsert по (user_id, recorded_date).
CREATE TABLE IF NOT EXISTS body_metrics (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    recorded_date   DATE NOT NULL,

    -- Показатели умных весов (извлекаются через vision LLM)
    weight          NUMERIC(5,2),            -- вес, кг
    body_fat_pct    NUMERIC(5,2),            -- жир, %
    muscle_kg       NUMERIC(5,2),            -- мышцы, кг
    water_pct       NUMERIC(5,2),            -- вода, %
    visceral_fat    NUMERIC(5,1),            -- висцеральный жир (индекс)
    bone_mass_kg    NUMERIC(4,2),            -- костная масса, кг
    bmr_kcal        NUMERIC(6,1),            -- BMR, ккал
    bmi             NUMERIC(4,1),            -- ИМТ

    -- Ручные замеры (сантиметры)
    arms_cm         NUMERIC(5,1),            -- руки, см
    thighs_cm       NUMERIC(5,1),            -- бёдра, см
    neck_cm         NUMERIC(5,1),            -- шея, см
    shin_cm         NUMERIC(5,1),            -- голень, см
    waist_cm        NUMERIC(5,1),            -- талия, см
    chest_cm        NUMERIC(5,1),            -- грудь, см
    hips_cm         NUMERIC(5,1),            -- ягодицы/бёдра, см

    source          TEXT NOT NULL DEFAULT 'manual',  -- 'scale_photo' | 'text' | 'manual'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (user_id, recorded_date)
);

-- Паттерны памяти: накопление доказательной базы перед верификацией пользователем
-- Флоу: Synthesizer замечает паттерн → pending (confirmations++)
--        → при confirmations >= 3: semantic filter → /memory верификация
--        → confirmed: переходит в memory_insights
--        → expired: не набрал подтверждений за TTL → удаляется
CREATE TABLE IF NOT EXISTS pending_insights (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pattern_text    TEXT NOT NULL,              -- формулировка паттерна
    confirmations   INT NOT NULL DEFAULT 1,     -- сколько раз паттерн замечен
    first_seen      DATE NOT NULL DEFAULT CURRENT_DATE,
    last_seen       DATE NOT NULL DEFAULT CURRENT_DATE,
    expires_at      DATE NOT NULL,              -- TTL: если не подтверждён — удаляется
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired'))
);

-- Подтверждённые паттерны: агент читает при каждом запросе
CREATE TABLE IF NOT EXISTS memory_insights (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    insight_text    TEXT NOT NULL,              -- паттерн, подтверждённый пользователем
    confirmed_at    DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Эмбеддинги сообщений для семантического поиска (pgvector)
-- Требует: CREATE EXTENSION vector; (миграция 001_pgvector.sql)
CREATE TABLE IF NOT EXISTS message_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536) NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_raw_events_source   ON raw_events(source);
CREATE INDEX IF NOT EXISTS idx_raw_events_created  ON raw_events(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session    ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_health_user_date    ON health_metrics(user_id, recorded_date);
CREATE INDEX IF NOT EXISTS idx_health_date         ON health_metrics(recorded_date);
CREATE INDEX IF NOT EXISTS idx_nutrition_user_date ON nutrition_logs(user_id, logged_date);
CREATE INDEX IF NOT EXISTS idx_body_user_date      ON body_metrics(user_id, recorded_date);
