-- ============================================================
-- AI_Assistant 完整数据库初始化脚本（含权限与多租户）
-- ============================================================

-- 1. pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 租户表
CREATE TABLE IF NOT EXISTS t_tenant (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(200) NOT NULL,
    code          VARCHAR(50) UNIQUE NOT NULL,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 3. 用户认证表（带租户与角色）
CREATE TABLE IF NOT EXISTS t_user (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR UNIQUE NOT NULL,
    password_hash VARCHAR,
    display_name  VARCHAR,
    role          VARCHAR(20) DEFAULT 'viewer',
    tenant_id     INT REFERENCES t_tenant(id),
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 4. 文档元数据表（带租户）
CREATE TABLE IF NOT EXISTS t_document (
    id            SERIAL PRIMARY KEY,
    doc_id        VARCHAR(12) UNIQUE NOT NULL,
    filename      VARCHAR(500),
    file_type     VARCHAR(20),
    file_size     BIGINT,
    pages         INT,
    parser_used   VARCHAR(50),
    chunks_count  INT,
    summary       TEXT,
    md5_hash      VARCHAR(32) UNIQUE,
    uploaded_at   TIMESTAMPTZ DEFAULT NOW(),
    user_id       INT,
    tenant_id     INT REFERENCES t_tenant(id)
);

-- 5. 会话信息表（带租户）
CREATE TABLE IF NOT EXISTS t_session_info (
    id          VARCHAR PRIMARY KEY,
    title       VARCHAR,
    user_id     INT,
    tenant_id   INT REFERENCES t_tenant(id),
    created_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ,
    summary     TEXT
);

-- 6. 会话消息表
CREATE TABLE IF NOT EXISTS t_session_message (
    id          SERIAL PRIMARY KEY,
    session_id  VARCHAR,
    role        VARCHAR,
    content     TEXT,
    created_at  TIMESTAMPTZ,
    summarized  BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_session_message_sid_id
    ON t_session_message (session_id, id);

-- 7. Sentence Window 父 chunk 表（带租户）
CREATE TABLE IF NOT EXISTS chunk_contexts (
    parent_id   TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    content     TEXT NOT NULL,
    filename    TEXT,
    chunk_index INTEGER,
    tenant_id   INT REFERENCES t_tenant(id)
);

CREATE INDEX IF NOT EXISTS idx_chunk_contexts_doc_id
    ON chunk_contexts (doc_id);
CREATE INDEX IF NOT EXISTS idx_chunk_contexts_tenant_id
    ON chunk_contexts (tenant_id);

-- 8. 文档摘要向量表（带租户）
CREATE TABLE IF NOT EXISTS doc_summaries (
    doc_id      TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    embedding   vector(1024),
    filename    TEXT,
    chunk_count INTEGER,
    tenant_id   INT REFERENCES t_tenant(id),
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_summaries_tenant_id
    ON doc_summaries (tenant_id);

-- 9. 向量库（chunk 存储，由 PGVectorStore 管理，tenant_id 放在 metadata_ JSONB 中）
CREATE TABLE IF NOT EXISTS data_documents (
    id               BIGINT PRIMARY KEY,
    text             VARCHAR,
    metadata_        JSONB,
    node_id          VARCHAR,
    embedding        VECTOR(1024),
    text_search_tsv  TSVECTOR
);
