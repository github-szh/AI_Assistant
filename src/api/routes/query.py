"""POST /query — RAG knowledge base Q&A endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.schemas import QueryRequest, QueryResponse, EvalResponse, VerdictDetail
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
    result = engine.query(
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
        for sse_line in engine.query_stream(
            question=req.question, top_k=req.top_k,
            doc_ids=req.doc_ids, messages=req.messages,
            tenant_id=user.get("tenant_id"),
        ):
            yield sse_line

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/eval", response_model=EvalResponse)
async def query_knowledge_eval(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(require_permission("quality:admin")),
):
    """Query the knowledge base with detailed quality evaluation (admin-only)."""
    logger.info("RAG eval: '%s' (top_k=%d, tenant=%s)", req.question[:100], req.top_k, user.get("tenant_id"))

    # 1. Get answer + sources + context
    eval_result = engine.query_eval(
        question=req.question, top_k=req.top_k,
        doc_ids=req.doc_ids, messages=req.messages,
        tenant_id=user.get("tenant_id"),
    )

    # 2. Run quality guard if available
    quality_dict = {}
    intervention_info = None
    if engine.quality_guard is not None:
        from src.config import settings
        if settings.quality_guard_enabled:
            ground_truth_kwargs = {}
            if req.ground_truth:
                ground_truth_kwargs["ground_truth"] = req.ground_truth

            _, intervention = engine.quality_guard.run(
                query=req.question,
                answer=eval_result["answer"],
                context=eval_result.get("context", ""),
                sources=eval_result["sources"],
                **ground_truth_kwargs,
            )
            intervention_info = intervention
            # Group verdicts by dimension
            for v in intervention.violations:
                dim = v.dimension or "unknown"
                quality_dict[dim] = VerdictDetail(
                    dimension=dim,
                    passed=v.passed,
                    score=v.score,
                    details=v.details or "",
                )

    return EvalResponse(
        answer=eval_result["answer"],
        sources=eval_result["sources"],
        quality=quality_dict,
        intervention=intervention_info,
    )
