"""Verify RRF fusion effectiveness — compare 3 retrieval paths side by side.

Usage: python verify_rrf.py "你的查询"
"""
import sys
sys.path.insert(0, ".")

from src.knowledge.retrieval import _bm25_search, _rrf_fusion, _get_original_text
from src.knowledge.embeddings import get_embedding_manager
from src.knowledge.index_store import get_vector_store
from src.knowledge.tokenizer import tokenize
from src.config import settings
from llama_index.core.vector_stores.types import VectorStoreQuery

K = 10  # how many results to show per path


def run(query: str):
    print(f"{'='*80}")
    print(f"  查询: {query}")
    print(f"{'='*80}")

    store = get_vector_store()
    embed_mgr = get_embedding_manager()
    query_emb = embed_mgr.encode_query(query)
    tokens = tokenize(query)

    # ── Path 1: Vector-only ──────────────────────────────────────
    q = VectorStoreQuery(query_embedding=query_emb, similarity_top_k=K, mode="default")
    result = store.query(q)
    dense_nodes = result.nodes or []
    if result.similarities:
        for n, s in zip(dense_nodes, result.similarities or []):
            if s is not None:
                object.__setattr__(n, 'score', s)

    print(f"\n{'─'*80}")
    print(f"  路 1 — 纯向量 (cosine similarity)")
    print(f"{'─'*80}")
    for i, n in enumerate(dense_nodes[:K]):
        score = getattr(n, 'score', 0) or 0
        text = _get_original_text(n)[:80].replace('\n', ' ')
        print(f"  [{i+1}] score={score:.4f}  {text}")

    # ── Path 2: BM25-only ────────────────────────────────────────
    sparse = _bm25_search(tokens, top_k=K)

    print(f"\n{'─'*80}")
    print(f"  路 2 — 纯 BM25 (PG ts_rank)")
    print(f"{'─'*80}")
    for i, r in enumerate(sparse[:K]):
        score = r['bm25_score']
        text = (r['metadata'].get('original_text', r['text']) or '')[:80].replace('\n', ' ')
        print(f"  [{i+1}] score={score:.4f}  {text}")
    if not sparse:
        print("  (无结果)")

    # ── Path 3: RRF fusion ───────────────────────────────────────
    # We need to match sparse node_ids back to actual TextNodes
    # For display, use the RRF score alongside dense/sparse ranks
    fused = _rrf_fusion(dense_nodes, sparse, top_k=K)

    print(f"\n{'─'*80}")
    print(f"  路 3 — RRF 融合 (k=60)")
    print(f"{'─'*80}")

    # Build dense rank lookup
    dense_rank = {}
    for rank, n in enumerate(dense_nodes):
        dense_rank[n.node_id] = rank + 1

    sparse_rank = {}
    for rank, r in enumerate(sparse):
        sparse_rank[r['node_id']] = rank + 1

    for i, n in enumerate(fused):
        rrf_score = getattr(n, 'score', 0) or 0
        dr = dense_rank.get(n.node_id, '-')
        sr = sparse_rank.get(n.node_id, '-')
        text = _get_original_text(n)[:80].replace('\n', ' ')
        # Mark: B=both paths, D=dense-only, S=sparse-only
        if n.node_id in dense_rank and n.node_id in sparse_rank:
            tag = "B"  # both
        elif n.node_id in dense_rank:
            tag = "D"  # dense only
        else:
            tag = "S"  # sparse only (would be LOST in old hybrid mode!)
        print(f"  [{i+1}] rrf={rrf_score:.4f}  dense_rank={dr}  sparse_rank={sr}  [{tag}]  {text}")

    # ── Summary ───────────────────────────────────────────────────
    dense_ids = {n.node_id for n in dense_nodes[:K]}
    sparse_ids = {r['node_id'] for r in sparse[:K]}
    fused_ids = {n.node_id for n in fused}
    both = dense_ids & sparse_ids
    dense_only = dense_ids - sparse_ids
    sparse_only = sparse_ids - dense_ids

    print(f"\n{'─'*80}")
    print(f"  重叠分析")
    print(f"{'─'*80}")
    print(f"  向量 top-{K}:         {len(dense_ids)} 条")
    print(f"  BM25  top-{K}:        {len(sparse_ids)} 条")
    print(f"  两路重合:             {len(both)} 条  ← RRF 加权提升")
    print(f"  仅向量命中:           {len(dense_only)} 条")
    print(f"  仅 BM25 命中:         {len(sparse_only)} 条  ← 旧 hybrid 会丢弃这些!")

    # Show sparse-only results that old hybrid would miss
    if sparse_only:
        print(f"\n  ⚠ 旧 hybrid 模式会丢弃的 BM25 独有结果:")
        for r in sparse[:K]:
            if r['node_id'] in sparse_only:
                text = (r['metadata'].get('original_text', r['text']) or '')[:100].replace('\n', ' ')
                print(f"    bm25={r['bm25_score']:.4f}  {text}")

    print()


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "年假有几天？"
    run(query)
