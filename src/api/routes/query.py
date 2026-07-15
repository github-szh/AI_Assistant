"""POST /query — RAG knowledge base Q&A endpoints."""

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.deps import get_pg_connection
from src.api.schemas import QueryRequest, QueryResponse, EvalResponse, VerdictDetail, InterventionInfo
from src.api.permissions import require_permission, get_effective_tenant_id
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
        tenant_id=get_effective_tenant_id(user),
    )
    # 保存问答到会话消息表
    if req.session_id:
        try:
            async with get_pg_connection() as conn:
                conn.execute(
                    "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                    [req.session_id, "user", req.question[:10000]],
                )
                answer_text = result.get("answer", "")
                conn.execute(
                    "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                    [req.session_id, "assistant", answer_text[:10000]],
                )
                conn.execute(
                    "UPDATE t_session_info SET updated_at=NOW() WHERE id=%s",
                    [req.session_id],
                )
                conn.commit()
        except Exception as exc:
            logger.warning("RAG query persist failed: %s", exc)

        # 自动摘要（best-effort）
        try:
            import asyncio as _asyncio
            from src.utils.summarizer import summarize_session
            _asyncio.create_task(_asyncio.to_thread(summarize_session, req.session_id))
        except Exception:
            pass

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
        full_answer = ""
        async for sse_line in engine.query_stream(
            question=req.question, top_k=req.top_k,
            doc_ids=req.doc_ids, messages=req.messages,
            tenant_id=get_effective_tenant_id(user),
        ):
            # 从 SSE 事件中收集回答文本
            if sse_line.startswith("data: "):
                try:
                    data = json.loads(sse_line[6:])
                    if "c" in data:
                        full_answer += data["c"]
                except (json.JSONDecodeError, KeyError):
                    pass
            yield sse_line

        # 流结束后保存问答到会话消息表
        if req.session_id:
            try:
                async with get_pg_connection() as conn:
                    conn.execute(
                        "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                        [req.session_id, "user", req.question[:10000]],
                    )
                    conn.execute(
                        "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                        [req.session_id, "assistant", full_answer[:10000]],
                    )
                    conn.execute(
                        "UPDATE t_session_info SET updated_at=NOW() WHERE id=%s",
                        [req.session_id],
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning("RAG stream persist failed: %s", exc)

            # 自动摘要（best-effort）
            try:
                import asyncio as _asyncio
                from src.utils.summarizer import summarize_session
                _asyncio.create_task(_asyncio.to_thread(summarize_session, req.session_id))
            except Exception:
                pass

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/eval", response_model=EvalResponse)
async def query_knowledge_eval(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(require_permission("quality:eval")),
):
    """Query the knowledge base with detailed quality evaluation (admin-only)."""
    logger.info("RAG eval: '%s' (top_k=%d, tenant=%s)", req.question[:100], req.top_k, user.get("tenant_id"))

    from src.config import settings

    # 前置安全审查：LLM Judge 审查用户输入（仅 /eval 端点）
    if settings.quality_guard_enabled:
        try:
            prompt_path = "prompts/quality/input_safety.yaml"
            with open(prompt_path, encoding="utf-8") as f:
                import yaml as _yaml
                prompt_template = _yaml.safe_load(f)["input_safety"]

            prompt = prompt_template.replace("{{ question }}", req.question)

            from src.llm.router import get_llm
            _llm = get_llm()
            response = _llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=settings.quality_judge_model,
                temperature=0.0,
                max_tokens=256,
            )

            import json as _json
            result = _json.loads(response)

            if not result.get("passed", True):
                logger.info(
                    "RAG eval input pre-check BLOCK: '%s' — reason: %s",
                    req.question[:100], result.get("reason", "")
                )
                return EvalResponse(
                    answer="",
                    sources=[],
                    quality={
                        "safety": VerdictDetail(
                            dimension="safety", passed=False,
                            score=0.0, details="输入安全审查未通过",
                        )
                    },
                    intervention=InterventionInfo(
                        intervened=True, action="block",
                        reason="输入内容不符合安全规范，已拦截",
                        violations=[],
                    ),
                )
            else:
                logger.debug("RAG eval input pre-check PASS: '%s'", req.question[:50])
        except Exception as exc:
            logger.warning("RAG eval input pre-check error, fail-open: %s", exc)

    # 1. Get answer + sources + context
    eval_result = await engine.query_eval(
        question=req.question, top_k=req.top_k,
        doc_ids=req.doc_ids, messages=req.messages,
        tenant_id=get_effective_tenant_id(user),
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
                # 没有标准答案时跳过 answer_correctness 维度（没有可对比的基准）
                if dim == "answer_correctness" and not req.ground_truth:
                    continue
                # 检索结果不足2条时跳过检索质量维度（统计指标无意义）
                if dim == "retrieval_quality" and len(eval_result["sources"]) < 2:
                    continue
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
