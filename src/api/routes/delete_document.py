"""DELETE /documents/{doc_id} — remove a document from the vector store."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_pg_connection
from src.api.permissions import require_permission
from src.api.schemas import DeleteDocumentResponse
from src.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)


@router.delete("/{doc_id}", response_model=DeleteDocumentResponse)
async def delete_document(doc_id: str, user: dict = Depends(require_permission("document:delete"))):
    """Delete a document from pgvector, t_document, and local filesystem."""
    # 权限与多租户：super_admin 跳过租户过滤（NULL = NULL 在 SQL 中永远为 false）
    is_super = user.get("role") == "super_admin"
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        if is_super:
            own = conn.execute(
                "SELECT 1 FROM t_document WHERE doc_id = %s", [doc_id],
            ).fetchone()
        else:
            own = conn.execute(
                "SELECT 1 FROM t_document WHERE doc_id = %s AND tenant_id = %s",
                [doc_id, tenant_id],
            ).fetchone()
        if not own:
            raise HTTPException(404, f"文档 {doc_id} 不存在或无权操作")

        if is_super:
            deleted_db = conn.execute(
                "DELETE FROM data_documents WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id') = %s",
                [doc_id],
            ).rowcount
            deleted_td = conn.execute(
                "DELETE FROM t_document WHERE doc_id = %s", [doc_id],
            ).rowcount
            conn.execute("DELETE FROM doc_summaries WHERE doc_id = %s", [doc_id])
            conn.execute("DELETE FROM chunk_contexts WHERE doc_id = %s", [doc_id])
        else:
            conn.execute(
                """DELETE FROM data_documents
                   WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id') = %s
                   AND metadata_->>'tenant_id' = %s""",
                [doc_id, str(tenant_id)],
            )
            deleted_td = conn.execute(
                "DELETE FROM t_document WHERE doc_id = %s AND tenant_id = %s",
                [doc_id, tenant_id],
            ).rowcount
            conn.execute(
                "DELETE FROM doc_summaries WHERE doc_id = %s AND tenant_id = %s",
                [doc_id, tenant_id],
            )
            conn.execute(
                "DELETE FROM chunk_contexts WHERE doc_id = %s AND tenant_id = %s",
                [doc_id, tenant_id],
            )
        conn.commit()

    deleted_fs = _delete_from_filesystem(doc_id)

    if not deleted_db and not deleted_td and not deleted_fs:
        raise HTTPException(404, f"文档 {doc_id} 不存在")

    return DeleteDocumentResponse(
        doc_id=doc_id,
        deleted=True,
        message=f"已从向量库删除 {deleted_db} 条记录，元数据表删除 {deleted_td} 条，本地文件 {'已清理' if deleted_fs else '未找到'}",
    )


def _delete_from_filesystem(doc_id: str) -> bool:
    """Delete the local file(s) matching doc_id."""
    docs_dir = Path(settings.data_dir) / "documents"
    if not docs_dir.exists():
        return False

    deleted = False
    for f in docs_dir.iterdir():
        if f.stem == doc_id:
            f.unlink()
            logger.info("Deleted local file: %s", f.name)
            deleted = True
    return deleted
