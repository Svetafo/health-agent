-- Migration 005: medical analyses
-- Three tables for storing medical study documents.
--
-- lab_sessions   — "envelope" for one lab document.
--                  One lab visit / one PDF = one row.
--                  Stores metadata: when, where, with what context.
--
-- lab_results    — each numeric value from the document = one row (EAV).
--                  References lab_sessions. Enables trends for any
--                  parameter (glucose, insulin, fsh...) over any period.
--
-- doctor_reports — text conclusions from doctors.
--                  Ultrasound, MRI, X-ray, mammography, cytology, smear.
--                  One document = one row. Stores the full protocol text
--                  and separately — the final conclusion.

CREATE TABLE IF NOT EXISTS lab_sessions (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    test_date   DATE        NOT NULL,
    lab_name    TEXT,                       -- "Invitro", "MedSwiss"
    source_file TEXT,                       -- filename or "photo.jpg"
    notes       TEXT,                       -- context: "after stopping metformin 2 months"
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_results (
    id              BIGSERIAL   PRIMARY KEY,
    session_id      BIGINT      REFERENCES lab_sessions(id) ON DELETE CASCADE,
    user_id         TEXT        NOT NULL,
    test_date       DATE        NOT NULL,   -- denormalized for fast queries
    parameter_name  TEXT        NOT NULL,   -- "Glucose" (as written in document)
    parameter_key   TEXT        NOT NULL,   -- "glucose" (standard key for queries)
    category        TEXT        NOT NULL,   -- "biochemistry" | "hormones" | "cbc" | ...
    value_numeric   NUMERIC,                -- 5.2 (NULL if value is like "< 37")
    value_text      TEXT,                   -- original string: "5.2", "< 37", "negative"
    unit            TEXT,                   -- "mmol/L", "mIU/L"
    ref_min         NUMERIC,                -- lower reference range
    ref_max         NUMERIC,                -- upper reference range
    ref_text        TEXT,                   -- if reference range is text, not numeric
    is_abnormal     BOOLEAN,                -- outside ref_min..ref_max range
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doctor_reports (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    study_date  DATE        NOT NULL,
    study_type  TEXT        NOT NULL,   -- "uzi" | "mrt" | "rentgen" | "mammografia" | "cytology" | "other"
    body_area   TEXT,                   -- "pelvis", "lumbar spine L4-S1", "shoulder"
    description TEXT,                   -- full protocol text
    conclusion  TEXT,                   -- final doctor conclusion only
    equipment   TEXT,                   -- device: "Philips IU 22", "MRI Ingenia 1.5T"
    doctor      TEXT,                   -- doctor name
    lab_name    TEXT,                   -- "MedSwiss", "Invitro"
    source_file TEXT,                   -- filename
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_sessions_user_date  ON lab_sessions   (user_id, test_date);
CREATE INDEX IF NOT EXISTS idx_lab_results_lookup      ON lab_results     (user_id, parameter_key, test_date);
CREATE INDEX IF NOT EXISTS idx_lab_results_category    ON lab_results     (user_id, category, test_date);
CREATE INDEX IF NOT EXISTS idx_doctor_reports_lookup   ON doctor_reports  (user_id, study_type, study_date);
