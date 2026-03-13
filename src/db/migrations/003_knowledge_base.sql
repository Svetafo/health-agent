-- migration: 003_knowledge_base.sql
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id         BIGSERIAL PRIMARY KEY,
    source     TEXT NOT NULL,        -- filename or URL
    title      TEXT,                 -- document title
    chunk_idx  INT  NOT NULL,        -- chunk index in document
    content    TEXT NOT NULL,        -- chunk text
    embedding  vector(1536) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kc_ivfflat ON knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
CREATE INDEX IF NOT EXISTS idx_kc_source ON knowledge_chunks(source);
