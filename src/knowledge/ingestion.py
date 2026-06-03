"""Ingestion — embed + tokenize + insert into pgvector.

Docs arrive pre-chunked from Chunker. No re-splitting here — that would
break the semantic boundaries Chunker already established.
"""

import logging

from src.parsing.loader import ParsedDocument

logger = logging.getLogger(__name__)


def ingest_documents(
    docs: list[ParsedDocument],
    doc_id: str = "",
    filename: str = "",
    extra_metadata: dict | None = None,
    parent_docs: list[ParsedDocument] | None = None,
    child_to_parent: dict[int, int] | None = None,
) -> int:
    """Embed via configured provider, insert into pgvector.

    Docs arrive pre-chunked from Chunker. No re-splitting here — that would
    break the semantic boundaries Chunker already established.
    """
    # 权限与多租户：ingest 时在 metadata 中写入 tenant_id
    if not docs:
        logger.warning("No documents to ingest")
        return 0

    from llama_index.core.schema import TextNode

    extra = extra_metadata or {}

    nodes = []
    for i, d in enumerate(docs):
        metadata = {
            "doc_id": doc_id,
            "source": doc_id,
            "filename": filename,
            "parser_used": d.parser_used,
            **extra,
            **d.metadata,
        }
        if child_to_parent is not None:
            metadata["parent_id"] = f"{doc_id}:{child_to_parent[i]}"
        node = TextNode(text=d.content, metadata=metadata)
        nodes.append(node)

    original_texts = [n.text for n in nodes]
    logger.info("Ingesting %d nodes for %s", len(original_texts), filename)

    from src.knowledge.embeddings import get_embedding_manager
    embed_mgr = get_embedding_manager()
    embeddings = embed_mgr.encode(original_texts)
    logger.info("Embedded %d vectors (dim=%d)", len(embeddings), len(embeddings[0]) if embeddings else 0)

    from src.knowledge.tokenizer import tokenize
    for node in nodes:
        node.metadata["original_text"] = node.text
        node.set_content(tokenize(node.text))

    from src.knowledge.index_store import get_vector_store
    store = get_vector_store()
    for node, emb in zip(nodes, embeddings):
        node.embedding = emb
    store.add(nodes)
    logger.info("Inserted %d nodes for doc_id=%s (%s)", len(nodes), doc_id, filename)

    if parent_docs:
        from src.knowledge.index_store import _ensure_chunk_contexts_table, _insert_parent_contexts
        _ensure_chunk_contexts_table()
        tenant_id = extra.get("tenant_id")
        rows = [
            {
                "parent_id": f"{doc_id}:{i}",
                "doc_id": doc_id,
                "content": d.content,
                "filename": filename,
                "chunk_index": d.metadata.get("chunk_index", i),
                "tenant_id": int(tenant_id) if tenant_id else None,
            }
            for i, d in enumerate(parent_docs)
        ]
        _insert_parent_contexts(rows)
        logger.info("Stored %d parent contexts for doc_id=%s", len(rows), doc_id)

    return len(nodes)
