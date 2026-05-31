"""DELETE /documents/{doc_id} — remove a document from the vector store."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_pg_connection
from src.api.routes.auth import get_current_user
from src.api.schemas import DeleteDocumentResponse
from src.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)


@router.delete("/{doc_id}", response_model=DeleteDocumentResponse)
async def delete_document(doc_id: str, user: dict = Depends(get_current_user)):
    """Delete a document from pgvector, t_document, and local filesystem."""
    conn = get_pg_connection()
    try:
        deleted_db = conn.execute(
            "DELETE FROM data_documents WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id') = %s",
            [doc_id],
        ).rowcount
        deleted_td = conn.execute(
            "DELETE FROM t_document WHERE doc_id = %s", [doc_id]
        ).rowcount
        # Cascade cleanup: summary index + sentence-window parent chunks
        conn.execute("DELETE FROM doc_summaries WHERE doc_id = %s", [doc_id])
        conn.execute("DELETE FROM chunk_contexts WHERE doc_id = %s", [doc_id])
        conn.commit()
    finally:
        conn.close()

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
