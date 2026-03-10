-- Migration 005: medical analyses
-- Три таблицы для хранения медицинских исследований.
--
-- lab_sessions   — «конверт» одного лабораторного документа.
--                  Один поход в лабораторию / один PDF = одна строка.
--                  Хранит метаданные: когда, где, с каким контекстом.
--
-- lab_results    — каждый числовой показатель из документа = одна строка (EAV).
--                  Ссылается на lab_sessions. Позволяет строить динамику по
--                  любому параметру (glucose, insulin, fsh...) за любой период.
--
-- doctor_reports — текстовые заключения врачей.
--                  УЗИ, МРТ, рентген, маммография, цитология, мазок.
--                  Один документ = одна строка. Хранит полный текст протокола
--                  и отдельно — итоговое заключение.

CREATE TABLE IF NOT EXISTS lab_sessions (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    test_date   DATE        NOT NULL,
    lab_name    TEXT,                       -- "Invitro", "MedSwiss"
    source_file TEXT,                       -- имя файла или "photo.jpg"
    notes       TEXT,                       -- контекст: "после отмены метформина 2 мес"
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_results (
    id              BIGSERIAL   PRIMARY KEY,
    session_id      BIGINT      REFERENCES lab_sessions(id) ON DELETE CASCADE,
    user_id         TEXT        NOT NULL,
    test_date       DATE        NOT NULL,   -- денормализовано для быстрых запросов
    parameter_name  TEXT        NOT NULL,   -- "Глюкоза" (как написано в документе)
    parameter_key   TEXT        NOT NULL,   -- "glucose" (стандартный ключ для запросов)
    category        TEXT        NOT NULL,   -- "biochemistry" | "hormones" | "cbc" | ...
    value_numeric   NUMERIC,                -- 5.2 (NULL если значение типа "< 37")
    value_text      TEXT,                   -- исходная строка: "5.2", "< 37", "отриц."
    unit            TEXT,                   -- "ммоль/л", "мЕд/л"
    ref_min         NUMERIC,                -- нижняя граница нормы
    ref_max         NUMERIC,                -- верхняя граница нормы
    ref_text        TEXT,                   -- если норма текстовая, а не числовая
    is_abnormal     BOOLEAN,                -- выходит за пределы ref_min..ref_max
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doctor_reports (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    study_date  DATE        NOT NULL,
    study_type  TEXT        NOT NULL,   -- "uzi" | "mrt" | "rentgen" | "mammografia" | "cytology" | "other"
    body_area   TEXT,                   -- "малый таз", "позвоночник L4-S1", "плечо"
    description TEXT,                   -- полный текст протокола исследования
    conclusion  TEXT,                   -- только итоговое заключение врача
    equipment   TEXT,                   -- аппарат: "Philips IU 22", "МРТ Ingenia 1.5T"
    doctor      TEXT,                   -- ФИО врача
    lab_name    TEXT,                   -- "MedSwiss", "Invitro"
    source_file TEXT,                   -- имя файла
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_sessions_user_date  ON lab_sessions   (user_id, test_date);
CREATE INDEX IF NOT EXISTS idx_lab_results_lookup      ON lab_results     (user_id, parameter_key, test_date);
CREATE INDEX IF NOT EXISTS idx_lab_results_category    ON lab_results     (user_id, category, test_date);
CREATE INDEX IF NOT EXISTS idx_doctor_reports_lookup   ON doctor_reports  (user_id, study_type, study_date);
