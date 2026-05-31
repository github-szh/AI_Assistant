"""Hybrid retrieval — independent vector + BM25 + RRF fusion + Reranker.

Two-stage retrieval:
1. Coarse: vector search and BM25 keyword search independently, then RRF fusion (top_k=20)
2. Fine:   bge-reranker-v2-m3 re-ranks to final top_k=5

Previously used pgvector's internal hybrid mode which prioritised dense results
and discarded sparse scores. Now the two ranking signals are fused properly with
Reciprocal Rank Fusion.
"""

import logging
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

def _bm25_search(query_tokens: str, top_k: int, doc_ids: list[str] | None = None) -> list[dict]:
    """BM25 keyword recall via PostgreSQL full-text search.

    query_tokens is jieba-segmented text (space-separated words).
    Uses OR logic (|) so partial token matches are scored by ts_rank
    rather than rejected outright. Punctuation-only tokens are filtered.
    When doc_ids is provided, restricts to those documents via metadata_->>'source'.
    """
    import psycopg
    # Filter to meaningful tokens, build OR-connected tsquery
    tokens = [t for t in query_tokens.split() if t.strip() and len(t.strip()) > 1 or t.strip().isalnum()]
    if not tokens:
        return []
    tsquery = " | ".join(tokens)

    # Build source filter clause
    source_filter = ""
    filter_params = []
    if doc_ids:
        placeholders = ",".join(["%s"] * len(doc_ids))
        source_filter = f"AND metadata_->>'source' IN ({placeholders})"
        filter_params = list(doc_ids)

    conn = psycopg.connect(settings.pg_dsn, connect_timeout=5)
    try:
        rows = conn.execute(
            f"""
            SELECT node_id, text, metadata_,
                   ts_rank(to_tsvector('simple', text), to_tsquery('simple', %s)) AS rank
            FROM data_documents
            WHERE to_tsvector('simple', text) @@ to_tsquery('simple', %s)
            {source_filter}
            ORDER BY rank DESC
            LIMIT %s
            """,
            [tsquery, tsquery] + filter_params + [top_k],
        ).fetchall()
        result = [
            {"node_id": r[0], "text": r[1], "metadata": r[2] or {}, "bm25_score": r[3]}
            for r in rows
        ]
        logger.debug("BM25召回: %d条", len(result))
        return result
    finally:
        conn.close()


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

def _expand_to_parents(child_nodes: list) -> list:
    """Expand child chunks to their parent context windows.

    Looks up parent_id in chunk_contexts table. Deduplicates — multiple
    children sharing the same parent return the parent only once.
    Preserves child node scores and _dense_max_score.

    If nodes have no parent_id (normal chunking mode), returns them as-is.
    """
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
    parents = _fetch_parent_contexts(parent_ids)

    if not parents:
        return child_nodes

    # Build result: preserve child order, deduplicate parents
    from llama_index.core.schema import TextNode

    result = []
    emitted = set()
    for node in child_nodes:
        pid = node.metadata.get("parent_id")
        if not pid or pid in emitted:
            continue
        emitted.add(pid)
        data = parents.get(pid)
        if not data:
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
        logger.debug("SW展开: %d子 → %d父 (去重%.0f%%)", len(child_nodes), len(result), dedup_pct)
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

    def _coarse_retrieve(self, query: str, doc_ids: list[str] | None = None) -> list:
        """Two independent recall paths + RRF fusion, returns ranked node list.

        When doc_ids is provided, restricts search to those documents via
        MetadataFilters (two-level retrieval Level 2 / external doc filter).
        """
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

        # Short-query boost: more candidates for BM25-heavy short queries
        qlen = len(query)
        coarse_k = (
            self.coarse_k + settings.retrieval_short_query_boost
            if qlen < settings.retrieval_short_query_len
            else self.coarse_k
        )

        # Build doc-id filter when requested (Level 2 of two-level retrieval)
        filters = None
        if doc_ids:
            filters = MetadataFilters(
                filters=[MetadataFilter(key="source", operator=FilterOperator.IN, value=doc_ids)],
                condition=FilterCondition.AND,
            )

        # Path 1: pure vector search (no hybrid — BM25 goes through separate path)
        q = VectorStoreQuery(
            query_embedding=query_embedding,
            similarity_top_k=coarse_k,
            mode="default",
            filters=filters,
        )
        result = store.query(q)
        dense_nodes = result.nodes or []

        # Attach similarity scores
        if result.similarities:
            for node, score in zip(dense_nodes, result.similarities):
                if score is not None:
                    object.__setattr__(node, 'score', score)

        logger.debug("向量召回: %d条", len(dense_nodes))

        if not dense_nodes:
            return []

        # Path 2: BM25 keyword search (graceful degradation when PG is down)
        try:
            sparse_results = _bm25_search(tokenized_query, top_k=coarse_k, doc_ids=doc_ids)
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

    def retrieve(self, query: str, doc_ids: list[str] | None = None) -> list:
        """Fast path: coarse retrieval → expand windows → top fine_k (no reranker)."""
        fused_nodes = self._coarse_retrieve(query, doc_ids=doc_ids)
        if not fused_nodes:
            return []
        expanded = _expand_to_parents(fused_nodes)
        return expanded[:self.fine_k]

    def retrieve_with_rerank(self, query: str, doc_ids: list[str] | None = None) -> list:
        """Deep path: coarse retrieval → expand windows → reranker → top fine_k."""
        fused_nodes = self._coarse_retrieve(query, doc_ids=doc_ids)
        if not fused_nodes:
            return []

        expanded = _expand_to_parents(fused_nodes)

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

        logger.debug("Reranker: %d → %d nodes", len(expanded), len(reranked_nodes))
        return reranked_nodes


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    return HybridRetriever()
