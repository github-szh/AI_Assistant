"""RAG Query Engine — retrieval + LLM generation.

Orchestrates the full RAG flow:
1. Retrieve relevant chunks via HybridRetriever
2. Build context from retrieved nodes
3. Generate answer via LLM (sync or streaming)
4. Return answer with source citations
"""

import hashlib
import json
import logging
import time
from functools import lru_cache

from src.config import settings
from src.llm.router import get_llm
from src.api.schemas import SourceInfo
from src.storage.cache import get_memory_cache  # 进程内缓存，相同问题5分钟内直接返回

logger = logging.getLogger(__name__)

_VECTOR_STORE_DOWN_MSG = (
    "向量数据库未就绪。请先安装依赖并启动 pgvector 或 ChromaDB。\n"
    "运行: pip install llama-index-vector-stores-postgres chromadb"
)


class QueryEngine:
    """End-to-end RAG query engine."""

    def __init__(self, retriever=None):
        self._retriever = retriever

    @staticmethod
    def _cache_key(question: str, top_k: int, doc_ids: list[str] | None = None) -> str:
        """根据问题内容生成缓存键（MD5哈希+top_k+doc_ids），确保相同问题命中同一缓存"""
        base = f"rag:{hashlib.md5(question.encode()).hexdigest()}:{top_k}"
        if doc_ids:
            base += ":" + ",".join(sorted(doc_ids))
        return base

    @property
    def retriever(self):
        if self._retriever is None:
            from src.knowledge.retrieval import get_retriever
            self._retriever = get_retriever()
        return self._retriever

    # ------------------------------------------------------------------
    # shared retrieval logic
    # ------------------------------------------------------------------

    def _rewrite_question(self, question: str, messages: list[dict] | None) -> str:
        """LLM 将依赖上下文的追问改写为独立问题。无历史时跳过。"""
        if not messages or len(messages) <= 1:
            return question

        recent = messages[-6:]  # 最近3轮对话
        try:
            llm = get_llm()
            history_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'AI'}: {m.get('content', '')[:200]}"
                for m in recent
            )
            rewritten = llm.chat(
                messages=[{"role": "user", "content": (
                    "根据对话历史，将用户的追问改写为一个独立的、不依赖上下文的查询问题。"
                    "只输出改写后的问题，不要输出任何其他内容。\n\n"
                    f"对话历史：\n{history_text}\n\n"
                    f"用户追问：{question}\n\n"
                    "改写后的问题："
                )}],
                temperature=0.0,
                max_tokens=100,
            )
            if rewritten and len(rewritten.strip()) > 2 and rewritten.strip() != question:
                logger.debug("查询改写: '%s' → '%s'", question, rewritten.strip())
                return rewritten.strip()
        except Exception:
            logger.debug("查询改写失败，使用原始查询")

        return question

    def _retrieve(self, question: str, top_k: int, doc_ids: list[str] | None = None,
                  messages: list[dict] | None = None) -> dict | None:
        """Two-level retrieval with automatic summary-based document pre-filtering.

        Level 1 (when enabled): search doc_summaries → top-3 relevant doc_ids.
        Level 2: search chunk index within those documents → RRF → expand → rerank.

        Skipped when KB has < two_stage_min_docs documents or doc_ids is already
        provided by the caller.

        When messages is provided, rewrites context-dependent follow-up questions
        into standalone queries before retrieval.
        """
        # ── Query rewriting for multi-turn conversations ──────────
        search_question = self._rewrite_question(question, messages)
        t_start = time.time()
        steps: list[dict] = []  # per-stage info for frontend display

        def _do_retrieve(search_query: str, deep: bool = False, ids: list[str] | None = None) -> list | None:
            try:
                if deep:
                    return self.retriever.retrieve_with_rerank(search_query, doc_ids=ids)
                return self.retriever.retrieve(search_query, doc_ids=ids)
            except (ImportError, ModuleNotFoundError) as exc:
                logger.warning("Vector store not available: %s", exc)
                return None

        # ── Level 1: document summary search ──────────────────────
        t_l1 = 0.0
        target_ids = doc_ids  # caller-provided (future UI), skip Level 1
        if target_ids is None:
            doc_count = _count_documents()
            if doc_count >= settings.two_stage_min_docs:
                try:
                    from src.knowledge.embeddings import get_embedding_manager
                    from src.knowledge.index_store import _search_summaries
                    emb_mgr = get_embedding_manager()
                    query_emb = emb_mgr.encode_query(search_question)
                    relevant = _search_summaries(query_emb, settings.summary_search_top_k)
                    if relevant:
                        target_ids = [r["doc_id"] for r in relevant]
                        t_l1 = time.time() - t_start
                        logger.debug("Level 1: %d summaries → %d docs selected (%.2fs)",
                                     doc_count, len(target_ids), t_l1)
                        steps.append({"label": "文档摘要搜索", "detail": f"从 {doc_count} 篇文档中定位 {len(target_ids)} 篇相关", "time": round(t_l1, 2)})
                except Exception:
                    logger.debug("Level 1 unavailable, falling back to full search")

        # ── Level 2: chunk search (with optional doc filter) ──────
        t_coarse_start = time.time()
        nodes = _do_retrieve(search_question, ids=target_ids)
        t_coarse = time.time() - t_coarse_start
        if nodes:
            steps.append({"label": "混合召回", "detail": f"向量搜索 + BM25 → RRF融合 → {len(nodes)} 条候选", "time": round(t_coarse, 2)})
        if nodes is None:
            return None  # vector store down
        if not nodes:
            return {"nodes": [], "sources": [], "context": "", "steps": steps}  # nothing found

        # Use dense similarity for threshold check when available (RRF fusion
        # scores are in a different range and would always trigger Stage 2).
        top_score = getattr(nodes[0], "_dense_max_score", None)
        if top_score is None:
            top_score = getattr(nodes[0], "score", 0) or 0

        logger.debug("Stage 1 top_score=%.3f (threshold=%.2f)", top_score, settings.retrieval_stage1_threshold)

        deep_search_used = False
        t_hyde = 0.0
        t_deep = 0.0
        if top_score <= settings.retrieval_stage1_threshold:
            steps.append({"label": "触发深度搜索", "detail": f"相似度 {top_score:.3f} ≤ 阈值 {settings.retrieval_stage1_threshold}", "time": None})
            logger.info("深度搜索: max_dense=%.3f≤%.2f → HyDE+Reranker", top_score, settings.retrieval_stage1_threshold)
            t_hyde_start = time.time()
            hyde_text = self._generate_hypothetical(search_question)
            t_hyde = time.time() - t_hyde_start
            if hyde_text:
                logger.debug("Stage 2 deep search (HyDE): %s (%.2fs)", hyde_text[:100], t_hyde)
                steps.append({"label": "HyDE 查询重写", "detail": f"LLM 生成假设答案辅助检索", "time": round(t_hyde, 2)})
                t_deep_start = time.time()
                nodes = _do_retrieve(hyde_text, deep=True, ids=target_ids)
                t_deep = time.time() - t_deep_start
                if not nodes:
                    return {"nodes": [], "sources": [], "context": "", "confidence": "low", "steps": steps}
                steps.append({"label": "Reranker 精排", "detail": f"Cross-encoder 重排序 → {len(nodes)} 条精选", "time": round(t_deep, 2)})
                deep_search_used = True
            else:
                steps.append({"label": "HyDE 降级", "detail": "生成失败，使用原始查询", "time": round(t_hyde, 2)})
                logger.warning("HyDE returned empty, deep search skipped — using original query results")
        else:
            steps.append({"label": "快路径", "detail": f"相似度 {top_score:.3f} > 阈值 {settings.retrieval_stage1_threshold}，跳过深度搜索", "time": None})
            logger.debug("快路径: max_dense=%.3f > %.2f, 跳过深度搜索", top_score, settings.retrieval_stage1_threshold)

        top_nodes = nodes[:top_k]

        context_parts = []
        sources = []
        for i, node in enumerate(top_nodes):
            content = node.metadata.get("original_text")
            if not content:
                logger.warning("Node %s missing original_text, using tokenized content", node.node_id)
                content = node.get_content()
            fname = node.metadata.get("filename", "")
            context_parts.append(f"[{i + 1}] ({fname})\n{content}")
            sources.append(SourceInfo(
                doc_id=node.metadata.get("doc_id", ""),
                filename=node.metadata.get("filename", ""),
                chunk_index=node.metadata.get("chunk_index"),
                score=round(getattr(node, "score", 0), 4) if getattr(node, "score", None) else None,
                snippet=content[:300],
            ))

        elapsed = time.time() - t_start

        if top_score >= 0.70:
            confidence = "high"
        elif top_score >= 0.50 or deep_search_used:
            confidence = "medium"
        else:
            confidence = "low"

        # build per-stage timing summary
        timing_parts = []
        if t_l1 > 0:
            timing_parts.append(f"L1={t_l1:.1f}s")
        timing_parts.append(f"粗召回={t_coarse:.1f}s")
        if t_hyde > 0:
            timing_parts.append(f"HyDE={t_hyde:.1f}s")
        if t_deep > 0:
            timing_parts.append(f"深度检索={t_deep:.1f}s")
        timing_str = ", ".join(timing_parts) if timing_parts else ""

        logger.info("检索完成: %d条来源, 置信度=%s, 总耗时 %.1fs (%s)",
                    len(sources), confidence, elapsed, timing_str)

        # ── 置信度过低时返回空结果，触发 LLM 兜底 ───────────
        if confidence == "low" and top_score < 0.40:
            logger.info("置信度过低(%.3f<0.40)，触发 LLM 兜底", top_score)
            return {"nodes": [], "sources": [], "context": "", "confidence": "low"}

        return {
            "nodes": top_nodes,
            "sources": sources,
            "context": "\n\n".join(context_parts),
            "confidence": confidence,
            "steps": steps,
        }

    # ------------------------------------------------------------------
    # sync query (kept for compatibility)
    # ------------------------------------------------------------------

    def query(self, question: str, top_k: int = 5, doc_ids: list[str] | None = None,
              messages: list[dict] | None = None) -> dict:
        """Answer a question using RAG — returns the complete answer at once."""
        # ── 缓存查询 ──────────────────────────────────────
        # 相同问题 5 分钟内直接返回缓存结果，避免重复调用 LLM
        cache = get_memory_cache()
        key = self._cache_key(question, top_k, doc_ids)
        cached = cache.get(key)
        if cached is not None:
            logger.debug("RAG 缓存命中: '%s'", question[:60])
            return cached

        result = self._retrieve(question, top_k, doc_ids, messages)
        if result is None:
            response = {"answer": _VECTOR_STORE_DOWN_MSG, "sources": []}
            cache.set(key, response, ttl=60)  # 缓存1分钟，避免重复检索
            return response
        if not result["nodes"]:
            response = {"answer": "知识库中没有找到相关信息。请先上传相关文档。", "sources": []}
            cache.set(key, response, ttl=60)  # 缓存1分钟，避免重复检索
            return response

        prompt = self._build_prompt(question, result["context"])
        llm = get_llm()
        answer = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1024,
        )
        response = {"answer": answer, "sources": result["sources"]}
        # ── 写入缓存 ──────────────────────────────────────
        # TTL=300 秒（5分钟），之后重新检索生成
        cache.set(key, response, ttl=300)
        logger.info("RAG 查询: '%s' → %d 条来源, 回答 %d 字", question, len(result["sources"]), len(answer))
        return response

    # ------------------------------------------------------------------
    # streaming query
    # ------------------------------------------------------------------

    def query_stream(self, question: str, top_k: int = 5, doc_ids: list[str] | None = None,
                      messages: list[dict] | None = None):
        """Answer a question using RAG — yields SSE JSON lines for streaming."""
        result = self._retrieve(question, top_k, doc_ids, messages)

        if result is None:
            yield f"data: {json.dumps({'error': _VECTOR_STORE_DOWN_MSG})}\n\n"
            return

        if not result["nodes"]:
            confidence = result.get("confidence", "low")
            yield f"data: {json.dumps({'step': 'not_found', 'msg': '知识库中没有找到相关信息。请先上传相关文档。', 'confidence': confidence})}\n\n"
            return

        # Push retrieval steps → sources → confidence so frontend can show the pipeline
        steps = result.get("steps", [])
        if steps:
            yield f"data: {json.dumps({'steps': steps})}\n\n"
        confidence = result.get("confidence", "medium")
        yield f"data: {json.dumps({'status': 'found'})}\n\n"
        yield f"data: {json.dumps({'sources': [s.model_dump() for s in result['sources']], 'confidence': confidence})}\n\n"

        # Stream LLM answer token by token
        prompt = self._build_prompt(question, result["context"])
        llm = get_llm()
        for chunk in llm.chat_stream(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1024,
        ):
            yield f"data: {json.dumps({'c': chunk})}\n\n"

        yield f"data: {json.dumps({'done': True})}\n\n"
        logger.info("RAG stream: '%s' → %d sources", question, len(result["sources"]))

    def _generate_hypothetical(self, question: str) -> str | None:
        """HyDE: ask LLM to write a hypothetical answer, improve retrieval recall."""
        try:
            llm = get_llm()
            return llm.chat(
                messages=[{"role": "user", "content": (
                    "请用一段话（50-100字）回答以下问题。不需要真实准确，只需要写出一个"
                    "看起来像答案的段落，用于帮助搜索引擎找到相关文档。\n问题：" + question
                )}],
                temperature=0.3,
                max_tokens=200,
            )
        except Exception as exc:
            logger.warning("HyDE generation failed: %s, falling back to original query", exc)
            return None

    def _build_prompt(self, question: str, context: str) -> str:
        from src.utils.prompt_loader import load_prompt
        return load_prompt("rag/query", context=context, question=question)


def _count_documents() -> int:
    """Count documents in the knowledge base (from t_document metadata table)."""
    try:
        import psycopg
        conn = psycopg.connect(settings.pg_dsn, connect_timeout=3)
        count = conn.execute("SELECT COUNT(*) FROM t_document").fetchone()[0]
        conn.close()
        return count
    except Exception as exc:
        logger.warning("文档计数失败: %s, 默认 0（跳过两级检索）", exc)
        return 0


@lru_cache(maxsize=1)
def get_query_engine() -> QueryEngine:
    return QueryEngine()
