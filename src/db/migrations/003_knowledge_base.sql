-- migration: 003_knowledge_base.sql
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id         BIGSERIAL PRIMARY KEY,
    source     TEXT NOT NULL,        -- имя файла или URL
    title      TEXT,                 -- заголовок документа
    chunk_idx  INT  NOT NULL,        -- порядковый номер чанка в документе
    content    TEXT NOT NULL,        -- текст чанка
    embedding  vector(1536) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kc_ivfflat ON knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
CREATE INDEX IF NOT EXISTS idx_kc_source ON knowledge_chunks(source);
