"""GET /documents — list documents + detail endpoint."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_pg_connection
from src.api.routes.auth import get_current_user
from src.api.schemas import DocumentInfo, DocumentDetail, DocumentListResponse
from src.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)


@router.get("", response_model=DocumentListResponse)
async def list_documents(user: dict = Depends(get_current_user)):
    try:
        docs = _query_pg_documents()
        if docs is not None:
            return DocumentListResponse(documents=docs[:50], total=len(docs))
        return DocumentListResponse(documents=[], total=0)
    except Exception as exc:
        logger.warning("Failed to list documents: %s", exc)
        return DocumentListResponse(documents=[], total=0)


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    docs = _query_pg_documents()
    if docs is None:
        raise HTTPException(503, "数据库暂不可用，请稍后重试")
    for d in docs:
        if d.doc_id == doc_id:
            chunks = _get_chunks(doc_id)
            return DocumentDetail(
                doc_id=d.doc_id, filename=d.filename, file_type=d.file_type,
                status=d.status, parser_used=d.parser_used,
                chunks_count=d.chunks_count, file_size=d.file_size,
                pages=d.pages, uploaded_at=d.uploaded_at, summary=d.summary,
                chunks=chunks,
            )
    raise HTTPException(404, "文档不存在")


def _query_pg_documents() -> list[DocumentInfo] | None:
    conn = None
    try:
        conn = get_pg_connection()
        rows = conn.execute("""
            SELECT
                td.doc_id,
                td.filename,
                td.file_type,
                td.parser_used,
                td.chunks_count,
                td.file_size,
                td.uploaded_at,
                td.pages,
                td.summary,
                count(dd.id) as vector_chunks
            FROM t_document td
            LEFT JOIN data_documents dd
                ON COALESCE(dd.metadata_->>'source', dd.metadata_->>'doc_id') = td.doc_id
            GROUP BY td.doc_id, td.filename, td.file_type, td.parser_used,
                     td.chunks_count, td.file_size, td.uploaded_at, td.pages, td.summary
            ORDER BY td.uploaded_at DESC
        """).fetchall()
    except Exception as exc:
        logger.warning("_query_pg_documents failed: %s", exc)
        return None
    finally:
        if conn:
            conn.close()

    docs = []
    for r in rows:
        doc_id = r[0] or ""
        filename = r[1] or "unknown"
        ext = r[2] or Path(filename).suffix
        parser = r[3] or "unknown"
        chunks = r[4] or 0
        size_raw = r[5]
        uploaded_raw = r[6]
        pages = r[7]
        summary = r[8] or ""

        if chunks > 0:
            status = "indexed"
        elif parser and parser != "unknown":
            status = "parse_failed"
        else:
            status = "no_text"

        docs.append(DocumentInfo(
            doc_id=doc_id, filename=filename, file_type=ext,
            status=status, parser_used=parser,
            chunks_count=chunks, file_size=_fmt_size(size_raw) if size_raw else "",
            pages=pages, uploaded_at=uploaded_raw.isoformat() if uploaded_raw else "",
            summary=summary[:300],
        ))
    return docs


def _get_chunks(doc_id: str) -> list[str]:
    conn = None
    try:
        conn = get_pg_connection()
        rows = conn.execute(
            "SELECT text FROM data_documents WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id')=%s ORDER BY id",
            [doc_id],
        ).fetchall()
        return [r[0][:500] for r in rows]
    except Exception as exc:
        logger.warning("获取文档片段失败: %s → %s", doc_id, exc)
        return []
    finally:
        if conn:
            conn.close()


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024: return f"{size_bytes} B"
    if size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"
