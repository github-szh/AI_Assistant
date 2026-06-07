"""Hybrid retrieval — independent vector + BM25 + RRF fusion + Reranker.

Two-stage retrieval:
1. Coarse: vector search and BM25 keyword search independently, then RRF fusion (top_k=20)
2. Fine:   bge-reranker-v2-m3 re-ranks to final top_k=5

Previously used pgvector's internal hybrid mode which prioritised dense results
and discarded sparse scores. Now the two ranking signals are fused properly with
Reciprocal Rank Fusion.
"""

import logging
import time
from functools import lru_cache

from src.config import settings

logger = logging.getLogger(__name__)


def _get_original_text(node) -> str:
    """Return original text for LLM/reranker, falling back to content with warning."""
    ot = node.metadata.get("original_text")
    if ot:
        return ot
    logger.warning("Node %s missing original_text, using tokenized content", node.node_id)
    return node.get_content()

# ---------------------------------------------------------------------------
# BM25 keyword search — bypasses llama_index, queries PG full-text index directly
# ---------------------------------------------------------------------------
async def _bm25_search(query_tokens: str, top_k: int, doc_ids: list[str] | None = None,
                 tenant_id: int | None = None) -> list[dict]:
    """BM25 keyword recall via PostgreSQL full-text search.

    query_tokens is jieba-segmented text (space-separated words).
    Uses OR logic (|) so partial token matches are scored by ts_rank
    rather than rejected outright. Punctuation-only tokens are filtered.
    When doc_ids is provided, restricts to those documents via metadata_->>'source'.
    """
    # Filter to meaningful tokens, build OR-connected tsquery
    # 权限与多租户：BM25 关键字搜索，按 tenant_id 过滤
    tokens = [t for t in query_tokens.split() if t.strip() and len(t.strip()) > 1 or t.strip().isalnum()]
    if not tokens:
        return []
    tsquery = " | ".join(tokens)
    # Build source filter clause
    conditions = []
    filter_params = []
    if doc_ids:
        placeholders = ",".join(["%s"] * len(doc_ids))
        conditions.append(f"metadata_->>'source' IN ({placeholders})")
        filter_params.extend(doc_ids)
    if tenant_id is not None:
        conditions.append("metadata_->>'tenant_id' = %s")
        filter_params.append(str(tenant_id))

    where_clause = ""
    if conditions:
        where_clause = "AND " + " AND ".join(conditions)

    t_bm25_inner = time.monotonic()
    from src.api.deps import get_pg_connection
    async with get_pg_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT node_id, text, metadata_,
                   ts_rank(to_tsvector('simple', text), to_tsquery('simple', %s)) AS rank
            FROM data_documents
            WHERE to_tsvector('simple', text) @@ to_tsquery('simple', %s)
            {where_clause}
            ORDER BY rank DESC
            LIMIT %s
            """,
            [tsquery, tsquery] + filter_params + [top_k],
        ).fetchall()
        result = [
            {"node_id": r[0], "text": r[1], "metadata": r[2] or {}, "bm25_score": r[3]}
            for r in rows
        ]
        logger.debug("BM25召回: %d条 (%.2fs)", len(result), time.monotonic() - t_bm25_inner)
        return result

# ---------------------------------------------------------------------------
# RRF (Reciprocal Rank Fusion) — fair score fusion across ranking sources
# ---------------------------------------------------------------------------
def _rrf_fusion(
    dense_nodes: list,
    sparse_results: list[dict],
    k: int = 60,
    top_k: int = 20,
) -> list:

    """Fuse two ranked lists with Reciprocal Rank Fusion.

    score(node) = Σ 1/(k + rank_i)  where rank_i is 0-indexed position.
    k=60 is the empirically optimal constant.
    """
    scores: dict[str, float] = {}
    node_map: dict[str, object] = {}
    # Dense contribution
    for rank, node in enumerate(dense_nodes):
        nid = node.node_id
        scores[nid] = scores.get(nid, 0) + 1.0 / (k + rank + 1)
        node_map[nid] = node
    # Sparse contribution
    for rank, row in enumerate(sparse_results):
        nid = row["node_id"]
        scores[nid] = scores.get(nid, 0) + 1.0 / (k + rank + 1)
        if nid not in node_map:
            # BM25-only result — reconstruct TextNode from DB row
            from llama_index.core.schema import TextNode
            meta = row["metadata"]
            original_text = meta.get("original_text", row["text"])
            node = TextNode(
                text=row["text"],
                id_=nid,
                metadata={**meta, "original_text": original_text},
            )
            node_map[nid] = node
    # Sort by RRF score descending
    sorted_ids = sorted(scores, key=lambda nid: scores[nid], reverse=True)
    result = []
    for nid in sorted_ids[:top_k]:
        node = node_map[nid]
        object.__setattr__(node, 'score', scores[nid])
        result.append(node)
    return result

# ---------------------------------------------------------------------------
# Sentence Window — expand child chunks to parent contexts
# ---------------------------------------------------------------------------
def _expand_to_parents(child_nodes: list, tenant_id: int | None = None) -> list:
    """Expand child chunks to their parent context windows.

    Looks up parent_id in chunk_contexts table. Deduplicates — multiple
    children sharing the same parent return the parent only once.
    Preserves child node scores and _dense_max_score.

    If nodes have no parent_id (normal chunking mode), returns them as-is.
    """
    t_sw = time.monotonic()
    if not child_nodes:
        return []
    # Check if any node has a parent_id
    parent_ids = []
    seen_pids = set()
    for node in child_nodes:
        pid = node.metadata.get("parent_id")
        if pid and pid not in seen_pids:
            seen_pids.add(pid)
            parent_ids.append(pid)

    if not parent_ids:
        return child_nodes  # normal mode — no expansion needed
    # Batch fetch parent contexts
    from src.knowledge.index_store import _fetch_parent_contexts
    parents = _fetch_parent_contexts(parent_ids, tenant_id=tenant_id)  # sync — uses get_pg_connection_sync

    if not parents:
        return child_nodes
    # Build result: preserve child order, deduplicate parents.
    # Nodes without parent_id (normal chunking) pass through as-is;
    # nodes with parent_id are expanded to parent context (deduplicated).
    from llama_index.core.schema import TextNode

    result = []
    emitted = set()
    for node in child_nodes:
        pid = node.metadata.get("parent_id")
        # Case 1: no parent_id — keep the original node
        if not pid:
            result.append(node)
            continue
        # Case 2: parent already emitted — skip (dedup)
        if pid in emitted:
            continue
        emitted.add(pid)
        data = parents.get(pid)
        # Case 3: parent not found in DB — fallback to original node
        if not data:
            result.append(node)
            continue

        parent = TextNode(
            text=data["content"],
            metadata={
                "doc_id": data.get("doc_id", ""),
                "filename": data.get("filename", ""),
                "original_text": data["content"],
                "chunk_index": data.get("chunk_index"),
                "parent_id": pid,
            },
        )
        # Inherit child scores
        object.__setattr__(parent, 'score', getattr(node, 'score', 0))
        dms = getattr(node, '_dense_max_score', None)
        if dms is not None:
            object.__setattr__(parent, '_dense_max_score', dms)
        result.append(parent)

    if parent_ids:
        dedup_pct = (len(child_nodes) - len(result)) / len(child_nodes) * 100
        logger.debug("SW展开: %d子 → %d父 (去重%.0f%%, %.2fs)", len(child_nodes), len(result), dedup_pct, time.monotonic() - t_sw)
    return result


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------
class HybridRetriever:
    """Two-stage retrieval: coarse (vector + BM25 → RRF) → reranker → final top_k."""

    def __init__(self, coarse_k: int | None = None, fine_k: int | None = None):
        self.coarse_k = coarse_k if coarse_k is not None else settings.retrieval_coarse_k
        self.fine_k = fine_k if fine_k is not None else settings.retrieval_fine_k

    # ------------------------------------------------------------------
    # shared coarse retrieval (was duplicated in retrieve / retrieve_with_rerank)
    # ------------------------------------------------------------------
    async def _coarse_retrieve(self, query: str, doc_ids: list[str] | None = None,
                         tenant_id: int | None = None) -> list:
        """Two independent recall paths + RRF fusion, returns ranked node list.

        When doc_ids is provided, restricts search to those documents via
        MetadataFilters (two-level retrieval Level 2 / external doc filter).
        """
        # 权限与多租户：粗召回阶段按 tenant_id 过滤
        from llama_index.core.vector_stores.types import (
            VectorStoreQuery, MetadataFilters, MetadataFilter,
            FilterOperator, FilterCondition,
        )
        from src.knowledge.embeddings import get_embedding_manager
        from src.knowledge.index_store import get_vector_store
        from src.knowledge.tokenizer import tokenize

        store = get_vector_store()
        embed_mgr = get_embedding_manager()
        # Embed & tokenize
        query_embedding = embed_mgr.encode_query(query)
        tokenized_query = tokenize(query)

        qlen = len(query)
        coarse_k = (
            self.coarse_k + settings.retrieval_short_query_boost
            if qlen < settings.retrieval_short_query_len
            else self.coarse_k
        )

        # 构建 metadata filters，包含 tenant_id 和 doc_ids
        filter_list = []
        if doc_ids:
            filter_list.append(MetadataFilter(key="source", operator=FilterOperator.IN, value=doc_ids))
        if tenant_id is not None:
            filter_list.append(MetadataFilter(key="tenant_id", operator=FilterOperator.EQ, value=str(tenant_id)))

        filters = None
        if filter_list:
            filters = MetadataFilters(
                filters=filter_list,
                condition=FilterCondition.AND,
            )
        # Path 1: pure vector search (no hybrid — BM25 goes through separate path)
        q = VectorStoreQuery(
            query_embedding=query_embedding,
            similarity_top_k=coarse_k,
            mode="default",
            filters=filters,
        )
        t_vec = time.monotonic()
        result = store.query(q)
        dense_nodes = result.nodes or []
        # Attach similarity scores
        if result.similarities:
            for node, score in zip(dense_nodes, result.similarities):
                if score is not None:
                    object.__setattr__(node, 'score', score)

        logger.debug("向量召回: %d条 (%.2fs)", len(dense_nodes), time.monotonic() - t_vec)

        if not dense_nodes:
            return []
        # Path 2: BM25 keyword search (graceful degradation when PG is down)
        try:
            sparse_results = await _bm25_search(tokenized_query, top_k=coarse_k, doc_ids=doc_ids, tenant_id=tenant_id)
        except Exception:
            logger.debug("BM25 search unavailable, using vector-only")
            return dense_nodes[:coarse_k]

        if not sparse_results:
            return dense_nodes[:coarse_k]
        # RRF fusion
        fused = _rrf_fusion(dense_nodes, sparse_results, top_k=coarse_k)
        # Preserve max dense similarity for Stage-1 threshold compatibility.
        # RRF scores (~0.01–0.03) are in a different range than cosine
        # similarity (0–1). query_engine uses _dense_max_score to decide
        # whether to trigger Stage 2 (HyDE + reranker).
        max_dense = max(
            (getattr(n, 'score', 0) or 0 for n in dense_nodes), default=0,
        )
        for node in fused:
            object.__setattr__(node, '_dense_max_score', max_dense)

        logger.debug(
            "RRF fusion: dense=%d + sparse=%d → fused=%d (max_dense=%.3f)",
            len(dense_nodes), len(sparse_results), len(fused), max_dense,
        )
        return fused

    # ------------------------------------------------------------------
    # public retrieval methods
    # ------------------------------------------------------------------
    async def retrieve(self, query: str, doc_ids: list[str] | None = None,
                 tenant_id: int | None = None) -> list:
        """Fast path: coarse retrieval → expand windows → top fine_k (no reranker)."""
        fused_nodes = await self._coarse_retrieve(query, doc_ids=doc_ids, tenant_id=tenant_id)
        if not fused_nodes:
            return []
        expanded = _expand_to_parents(fused_nodes, tenant_id=tenant_id)
        return expanded[:self.fine_k]

    async def retrieve_with_rerank(self, query: str, doc_ids: list[str] | None = None,
                              tenant_id: int | None = None) -> list:
        """Deep path: coarse retrieval → expand windows → reranker → top fine_k."""
        fused_nodes = await self._coarse_retrieve(query, doc_ids=doc_ids, tenant_id=tenant_id)
        if not fused_nodes:
            return []

        expanded = _expand_to_parents(fused_nodes, tenant_id=tenant_id)

        # 按 RRF 分数排序后截断，控制 Reranker 候选数以降低延迟
        if len(expanded) > settings.retrieval_max_rerank_candidates:
            expanded.sort(key=lambda n: getattr(n, 'score', 0), reverse=True)
            expanded = expanded[:settings.retrieval_max_rerank_candidates]

        _rag_start = time.monotonic()
        if settings.rerank_enabled:
            from src.knowledge.reranker import get_reranker
            reranker = get_reranker()
            candidates = [_get_original_text(n) for n in expanded]
            ranked = reranker.rerank(
                query, candidates,
                top_k=self.fine_k,
                min_score=settings.rerank_min_score,
            )
        else:
            return expanded[:self.fine_k]
        # O(1) node lookup via text → [nodes] mapping (handles duplicate text)
        from collections import defaultdict
        text_to_nodes: dict[str, list] = defaultdict(list)
        for node in expanded:
            text_to_nodes[_get_original_text(node)].append(node)

        reranked_nodes = []
        for text, score in ranked:
            if text is None:
                # reranker disabled: use expanded node directly
                node = expanded[len(reranked_nodes)]
                object.__setattr__(node, 'score', 0.0)
                reranked_nodes.append(node)
                continue
            nodes = text_to_nodes.get(text)
            if nodes:
                node = nodes.pop(0)
                object.__setattr__(node, 'score', score)
                reranked_nodes.append(node)

        # Min-max 归一化 BGE logits 到 [0, 1]，确保分数在统一量纲
        # 同时标记 _reranked，供下游 query_engine 区分快路径 (RRF) 和深度路径 (BGE)
        if reranked_nodes:
            all_scores = [getattr(n, 'score', 0.0) for n in reranked_nodes]
            s_min, s_max = min(all_scores), max(all_scores)
            for n in reranked_nodes:
                s = getattr(n, 'score', 0.0)
                if s_max > s_min:
                    object.__setattr__(n, 'score', (s - s_min) / (s_max - s_min))
                else:
                    object.__setattr__(n, 'score', 0.5)  # 单条结果，中性分
                object.__setattr__(n, '_reranked', True)

        logger.debug("Reranker: %d → %d nodes", len(expanded), len(reranked_nodes))

        # Record RAG quality metrics
        try:
            from src.monitoring.storage import save_rag_query
            scores = [s for _, s in ranked if s is not None]
            max_score = max(scores) if scores else 0.0
            query_hash = str(hash(query))
            save_rag_query(query_hash, query, max_score, len(reranked_nodes), time.monotonic() - _rag_start, tenant_id)
        except Exception:
            pass

        return reranked_nodes


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    return HybridRetriever()
