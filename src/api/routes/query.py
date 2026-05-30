"""POST /query — RAG knowledge base Q&A endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.schemas import QueryRequest, QueryResponse
from src.api.routes.auth import get_current_user
from src.knowledge.query_engine import QueryEngine, get_query_engine

router = APIRouter(prefix="/query", tags=["query"])
logger = logging.getLogger(__name__)


@router.post("", response_model=QueryResponse)
async def query_knowledge(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(get_current_user),
):
    """Query the knowledge base (non-streaming)."""
    logger.info("RAG query: '%s' (top_k=%d)", req.question[:100], req.top_k)
    result = engine.query(question=req.question, top_k=req.top_k, doc_ids=req.doc_ids)
    return QueryResponse(**result)


@router.post("/stream")
async def query_knowledge_stream(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(get_current_user),
):
    """Query the knowledge base with SSE streaming — sources first, then tokens."""
    logger.info("RAG stream: '%s' (top_k=%d)", req.question[:100], req.top_k)

    async def generate():
        for sse_line in engine.query_stream(question=req.question, top_k=req.top_k, doc_ids=req.doc_ids):
            yield sse_line

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
