"""POST /query — RAG knowledge base Q&A endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.schemas import QueryRequest, QueryResponse
from src.api.permissions import require_permission
from src.knowledge.query_engine import QueryEngine, get_query_engine

router = APIRouter(prefix="/query", tags=["query"])
logger = logging.getLogger(__name__)


@router.post("", response_model=QueryResponse)
async def query_knowledge(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(require_permission("knowledge:query")),
):
    """Query the knowledge base (non-streaming)."""
    # 权限与多租户：按租户隔离，注入 tenant_id
    logger.info("RAG query: '%s' (top_k=%d, tenant=%s)", req.question[:100], req.top_k, user.get("tenant_id"))
    result = await engine.query(
        question=req.question, top_k=req.top_k,
        doc_ids=req.doc_ids, messages=req.messages,
        tenant_id=user.get("tenant_id"),
    )
    return QueryResponse(**result)


@router.post("/stream")
async def query_knowledge_stream(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(require_permission("knowledge:query")),
):
    """Query the knowledge base with SSE streaming — sources first, then tokens."""
    # 权限与多租户：按租户隔离，注入 tenant_id
    logger.info("RAG stream: '%s' (top_k=%d, tenant=%s)", req.question[:100], req.top_k, user.get("tenant_id"))

    async def generate():
        async for sse_line in engine.query_stream(
            question=req.question, top_k=req.top_k,
            doc_ids=req.doc_ids, messages=req.messages,
            tenant_id=user.get("tenant_id"),
        ):
            yield sse_line

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
