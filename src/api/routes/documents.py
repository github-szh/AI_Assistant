"""GET /documents — list documents + detail endpoint."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from src.api.deps import get_pg_connection
from src.api.permissions import require_permission
from src.api.schemas import DocumentInfo, DocumentDetail, DocumentListResponse
from src.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)


@router.get("", response_model=DocumentListResponse)
async def list_documents(user: dict = Depends(require_permission("document:view"))):
    try:
        docs = await _query_pg_documents(user)  # 权限与多租户：传入 user 按 tenant_id 过滤
        if docs is not None:
            return DocumentListResponse(documents=docs[:50], total=len(docs))
        return DocumentListResponse(documents=[], total=0)
    except Exception as exc:
        logger.warning("Failed to list documents: %s", exc)
        return DocumentListResponse(documents=[], total=0)


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str, user: dict = Depends(require_permission("document:view"))):
    docs = await _query_pg_documents(user)  # 权限与多租户：按租户过滤
    if docs is None:
        raise HTTPException(503, "数据库暂不可用，请稍后重试")
    for d in docs:
        if d.doc_id == doc_id:
            chunks = await _get_chunks(doc_id)
            return DocumentDetail(
                doc_id=d.doc_id, filename=d.filename, file_type=d.file_type,
                status=d.status, parser_used=d.parser_used,
                chunks_count=d.chunks_count, file_size=d.file_size,
                pages=d.pages, uploaded_at=d.uploaded_at, summary=d.summary,
                chunks=chunks,
            )
    raise HTTPException(404, "文档不存在")


async def _query_pg_documents(user: dict) -> list[DocumentInfo] | None:
    # 权限与多租户：super_admin 看全部，其他角色按 tenant_id 过滤
    is_super = user.get("role") == "super_admin"
    tenant_id = user.get("tenant_id")
    try:
        async with get_pg_connection() as conn:
            if is_super:
                rows = conn.execute("""
                    SELECT td.doc_id, td.filename, td.file_type, td.parser_used,
                           td.chunks_count, td.file_size, td.uploaded_at, td.pages,
                           td.summary, td.chunk_strategy, count(dd.id) as vector_chunks,
                           COALESCE(u.display_name, u.username) as uploaded_by
                    FROM t_document td
                    LEFT JOIN data_documents dd
                        ON COALESCE(dd.metadata_->>'source', dd.metadata_->>'doc_id') = td.doc_id
                    LEFT JOIN t_user u ON td.user_id = u.id
                    GROUP BY td.doc_id, td.filename, td.file_type, td.parser_used,
                             td.chunks_count, td.file_size, td.uploaded_at, td.pages,
                             td.summary, td.chunk_strategy, u.display_name, u.username
                    ORDER BY td.uploaded_at DESC
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT td.doc_id, td.filename, td.file_type, td.parser_used,
                           td.chunks_count, td.file_size, td.uploaded_at, td.pages,
                           td.summary, td.chunk_strategy, count(dd.id) as vector_chunks,
                           COALESCE(u.display_name, u.username) as uploaded_by
                    FROM t_document td
                    LEFT JOIN data_documents dd
                        ON COALESCE(dd.metadata_->>'source', dd.metadata_->>'doc_id') = td.doc_id
                    LEFT JOIN t_user u ON td.user_id = u.id
                    WHERE td.tenant_id = %s
                    GROUP BY td.doc_id, td.filename, td.file_type, td.parser_used,
                             td.chunks_count, td.file_size, td.uploaded_at, td.pages,
                             td.summary, td.chunk_strategy, u.display_name, u.username
                    ORDER BY td.uploaded_at DESC
                """, [tenant_id]).fetchall()
    except Exception as exc:
        logger.warning("_query_pg_documents failed: %s", exc)
        return None

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
        chunk_strategy = r[9] or ""
        uploaded_by = r[11] or ""

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
            summary=summary[:300], uploaded_by=uploaded_by, chunk_strategy=chunk_strategy,
        ))
    return docs


@router.get("/{doc_id}/download")
async def download_document(doc_id: str, user: dict = Depends(require_permission("document:download"))):
    """Download the original uploaded file."""
    is_super = user.get("role") == "super_admin"
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        if is_super:
            row = conn.execute(
                "SELECT filename, file_type FROM t_document WHERE doc_id = %s",
                [doc_id],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT filename, file_type FROM t_document WHERE doc_id = %s AND tenant_id = %s",
                [doc_id, tenant_id],
            ).fetchone()

    if not row:
        logger.warning("文件下载失败, doc_id=%s 不存在或无权访问", doc_id)
        raise HTTPException(404, "文档不存在")

    filename, file_type = row
    ext = file_type or Path(filename).suffix
    file_path = Path(settings.data_dir) / "documents" / f"{doc_id}{ext}"

    if not file_path.is_file():
        logger.warning("文件下载失败, 原始文件不存在: %s", file_path)
        raise HTTPException(404, "原始文件不存在，可能已被清理")

    logger.info("文件 %s 下载成功 (doc_id=%s)", filename, doc_id)
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


async def _get_chunks(doc_id: str) -> list[str]:
    try:
        async with get_pg_connection() as conn:
            rows = conn.execute(
                "SELECT text FROM data_documents WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id')=%s ORDER BY id",
                [doc_id],
            ).fetchall()
            return [r[0][:500] for r in rows]
    except Exception as exc:
        logger.warning("获取文档片段失败: %s → %s", doc_id, exc)
        return []


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024: return f"{size_bytes} B"
    if size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"
