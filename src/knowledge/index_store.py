"""Vector store abstraction over pgvector, with ChromaDB fallback.

Strategy: try pgvector first, fall back to ChromaDB (local file) when PG is unreachable.
Both implement the same LlamaIndex VectorStore interface, so retrieval code doesn't care.

All heavy deps (llama_index, pgvector, chromadb, psycopg) are lazy-loaded.
All DB connections go through the shared pool (with get_pg_connection() as conn).
"""

import logging
from functools import lru_cache
from typing import Any

from src.api.deps import get_pg_connection_sync
from src.config import settings

logger = logging.getLogger(__name__)


def create_vector_store() -> Any:
    """Create a vector store instance, preferring pgvector over ChromaDB.

    pgvector: connects to PostgreSQL. Requires PG_HOST/PG_PORT/etc in .env.
    ChromaDB: stores vectors in a local directory. Zero-config fallback.
    """
    pg_available = _check_pgvector()

    if pg_available:
        logger.info("Using pgvector at %s:%d", settings.pg_host, settings.pg_port)
        return _create_pgvector_store()
    else:
        logger.info("pgvector unavailable, using ChromaDB (local fallback)")
        return _create_chroma_store()


def _check_pgvector() -> bool:
    """Check if pgvector is reachable."""
    try:
        with get_pg_connection_sync() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _create_pgvector_store() -> Any:
    from llama_index.vector_stores.postgres import PGVectorStore

    # PGVectorStore 是 llama-index 对 pgvector 的封装，
    # 自动管理 data_documents 表的创建和向量增删查
    store = PGVectorStore(
        connection_string=settings.pg_dsn,
        async_connection_string=settings.pg_async_dsn,
        table_name="documents",
        embed_dim=1024,  # bge-large-zh-v1.5
        schema_name="public",
        hybrid_search=True,
        text_search_config="simple",
    )
    _init_pgvector_schema()
    return store


def _init_pgvector_schema() -> None:
    """Ensure pgvector extension is available and create ivfflat index."""
    try:
        with get_pg_connection_sync() as conn:
            # putconn 归还时已设 autocommit=True，连接处于 IDLE 状态
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_data_documents_embedding
                ON data_documents
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)
        logger.info("pgvector extension + ivfflat index ensured")
    except Exception as exc:
        logger.warning("Failed to init pgvector schema/index: %s", exc)


def _create_chroma_store() -> Any:
    import chromadb
    from llama_index.vector_stores.chroma import ChromaVectorStore

    chroma_path = f"{settings.data_dir}/chroma_db"
    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_or_create_collection("ai_documents")
    return ChromaVectorStore(chroma_collection=collection)

# ---------------------------------------------------------------------------
# chunk_contexts — parent chunks for Sentence Window Retrieval
# ---------------------------------------------------------------------------
def _ensure_chunk_contexts_table() -> None:
    """Create the parent-chunk lookup table (no vector — plain text storage)."""
    try:
        with get_pg_connection_sync() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunk_contexts (
                    parent_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
                    content TEXT NOT NULL, filename TEXT,
                    chunk_index INTEGER, tenant_id INT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunk_contexts_doc_id
                ON chunk_contexts (doc_id)
            """)
    except Exception as exc:
        logger.warning("Failed to ensure chunk_contexts table: %s", exc)


def _insert_parent_contexts(rows: list[dict]) -> None:
    """Batch-insert parent chunks into chunk_contexts."""
    if not rows:
        return
    try:
        with get_pg_connection_sync() as conn:
            for r in rows:
                conn.execute(
                    """INSERT INTO chunk_contexts (parent_id, doc_id, content, filename, chunk_index, tenant_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (parent_id) DO NOTHING""",
                    [r["parent_id"], r["doc_id"], r["content"], r.get("filename", ""),
                     r.get("chunk_index", 0), r.get("tenant_id")],
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to insert parent contexts: %s", exc)

def _fetch_parent_contexts(parent_ids: list[str], tenant_id: int | None = None) -> dict[str, dict]:
    """Batch-fetch parent chunks by ID. Returns {parent_id: {content, doc_id, ...}}."""
    if not parent_ids:
        return {}
    try:
        with get_pg_connection_sync() as conn:
            if tenant_id is not None:
                rows = conn.execute(
                    "SELECT parent_id, doc_id, content, filename, chunk_index FROM chunk_contexts WHERE parent_id = ANY(%s) AND tenant_id = %s",
                    [parent_ids, tenant_id],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT parent_id, doc_id, content, filename, chunk_index FROM chunk_contexts WHERE parent_id = ANY(%s)",
                    [parent_ids],
                ).fetchall()
            return {
                r[0]: {"doc_id": r[1], "content": r[2], "filename": r[3], "chunk_index": r[4]}
                for r in rows
            }
    except Exception as exc:
        logger.warning("父块获取失败: %d IDs → %s", len(parent_ids), exc)
        return {}

# ---------------------------------------------------------------------------
# doc_summaries — document-level summary index for two-level retrieval
# ---------------------------------------------------------------------------
def _ensure_summary_collection() -> None:
    """Create the document-summary vector table."""
    try:
        with get_pg_connection_sync() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doc_summaries (
                    doc_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    embedding vector(1024),
                    filename TEXT,
                    chunk_count INTEGER,
                    tenant_id INT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
    except Exception as exc:
        logger.warning("Failed to ensure doc_summaries table: %s", exc)


def _insert_summary(
    doc_id: str, summary: str, embedding: list[float],
    filename: str = "", chunk_count: int = 0, tenant_id: int | None = None,
) -> None:
    """Insert or update a document summary with its embedding."""
    try:
        with get_pg_connection_sync() as conn:
            conn.execute(
                """INSERT INTO doc_summaries (doc_id, summary, embedding, filename, chunk_count, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (doc_id) DO UPDATE
                   SET summary=EXCLUDED.summary, embedding=EXCLUDED.embedding,
                       filename=EXCLUDED.filename, chunk_count=EXCLUDED.chunk_count,
                       tenant_id=EXCLUDED.tenant_id""",
                [doc_id, summary, embedding, filename, chunk_count, tenant_id],
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to insert summary for %s: %s", doc_id, exc)


def _search_summaries(query_embedding: list[float], top_k: int = 3,
                       tenant_id: int | None = None) -> list[dict]:
    """Level 1: search document summaries, return top-k relevant doc info."""
    try:
        with get_pg_connection_sync() as conn:
            if tenant_id is not None:
                rows = conn.execute(
                    """
                    SELECT doc_id, summary, filename, chunk_count,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM doc_summaries
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    [query_embedding, tenant_id, query_embedding, top_k],
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT doc_id, summary, filename, chunk_count,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM doc_summaries
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    [query_embedding, query_embedding, top_k],
                ).fetchall()
            return [
                {"doc_id": r[0], "summary": r[1], "filename": r[2],
                 "chunk_count": r[3], "similarity": r[4]}
                for r in rows
            ]
    except Exception as exc:
        logger.warning("摘要搜索失败: %s, 降级为全库搜索", exc)
        return []


@lru_cache(maxsize=1)
def get_vector_store() -> Any:
    return create_vector_store()
