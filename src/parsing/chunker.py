"""Text chunking strategies for downstream RAG ingestion."""

import re
from dataclasses import dataclass
from typing import Iterator

from src.parsing.loader import ParsedDocument


@dataclass
class ChunkResult:
    """Parent-child chunk pairs for Sentence Window Retrieval.

    index_chunks:   small chunks (3 sentences) — embedded for vector search
    context_chunks: large chunks (15 sentences) — stored for LLM context
    index_to_parent: maps index_chunks[i] → context_chunks[j]
    """
    index_chunks: list[ParsedDocument]
    context_chunks: list[ParsedDocument]
    index_to_parent: dict[int, int]


class Chunker:
    """Split parsed documents into smaller chunks for embedding.

    Supports:
    - fixed_size: split by character count with overlap
    - sentence: split on sentence boundaries (Chinese + English)
    - markdown_header: split on markdown headings (##, ###)
    - recursive: hierarchical fallback: paragraph → sentence → fixed_size.
      Preserves semantic boundaries as much as possible.
    """

    def __init__(
        self,
        strategy: str = "sentence",
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        sentence_window: bool = False,
    ):
        self.strategy = strategy
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.sentence_window = sentence_window

    def chunk(self, docs: list[ParsedDocument]) -> list[ParsedDocument]:
        chunks = []
        for doc in docs:
            texts = self._split(doc.content)
            for i, text in enumerate(texts):
                chunks.append(ParsedDocument(
                    file_path=doc.file_path,
                    file_type=doc.file_type,
                    content=text,
                    metadata={
                        **doc.metadata,
                        "chunk_index": i,
                    },
                    parser_used=doc.parser_used,
                ))
        return chunks

    # ------------------------------------------------------------------
    # Sentence Window — parent-child chunking (index + context layers)
    # ------------------------------------------------------------------

    _SENTENCES_PER_CHILD = 3
    _SENTENCES_PER_PARENT = 15

    def chunk_with_windows(self, docs: list[ParsedDocument]) -> ChunkResult:
        """Produce parent-child chunk pairs for Sentence Window Retrieval.

        Only sentence and recursive strategies support windowed output.
        Other strategies fall back to index = context (no window expansion).
        """
        if self.strategy not in ("sentence", "recursive"):
            # Fallback: child = parent for strategies that don't split on sentences
            flat = self.chunk(docs)
            return ChunkResult(
                index_chunks=flat,
                context_chunks=flat,
                index_to_parent={i: i for i in range(len(flat))},
            )

        index_chunks = []
        context_chunks = []
        index_to_parent: dict[int, int] = {}

        for doc in docs:
            child_texts, parent_texts, mapping = self._split_with_windows(doc.content)
            base_child = len(index_chunks)
            base_parent = len(context_chunks)
            for i, text in enumerate(child_texts):
                index_chunks.append(ParsedDocument(
                    file_path=doc.file_path, file_type=doc.file_type,
                    content=text,
                    metadata={**doc.metadata, "chunk_index": i},
                    parser_used=doc.parser_used,
                ))
            for i, text in enumerate(parent_texts):
                context_chunks.append(ParsedDocument(
                    file_path=doc.file_path, file_type=doc.file_type,
                    content=text,
                    metadata={**doc.metadata, "chunk_index": i},
                    parser_used=doc.parser_used,
                ))
            for child_i, parent_i in mapping.items():
                index_to_parent[base_child + child_i] = base_parent + parent_i

        return ChunkResult(index_chunks, context_chunks, index_to_parent)

    def _split_with_windows(self, text: str) -> tuple[list[str], list[str], dict[int, int]]:
        """Route to strategy-specific window splitter."""
        if self.strategy == "sentence":
            return self._sentence_split_with_windows(text)
        else:
            return self._recursive_split_with_windows(text)

    def _sentence_split_with_windows(
        self, text: str,
    ) -> tuple[list[str], list[str], dict[int, int]]:
        """Sentence strategy: children = 3 sentences, parents = 15 sentences."""
        sentences = re.split(r"(?<=[。！？.!?\n])\s*", text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return [], [], {}

        children: list[str] = []
        parents: list[str] = []
        child_to_parent: dict[int, int] = {}

        sc = self._SENTENCES_PER_CHILD
        sp = self._SENTENCES_PER_PARENT

        # Build child chunks
        for i in range(0, len(sentences), sc):
            children.append("".join(sentences[i:i + sc]))

        # Build parent chunks, map children to parents
        parent_idx = 0
        for p_start in range(0, len(sentences), sp):
            p_end = min(len(sentences), p_start + sp)
            parents.append("".join(sentences[p_start:p_end]))
            # All children within this parent's range
            child_start = p_start // sc
            child_end = (p_end + sc - 1) // sc  # ceil division
            for ci in range(child_start, min(child_end, len(children))):
                child_to_parent[ci] = parent_idx
            parent_idx += 1

        return children, parents, child_to_parent

    def _recursive_split_with_windows(
        self, text: str,
    ) -> tuple[list[str], list[str], dict[int, int]]:
        """Recursive strategy: paragraphs as children, grouped as parents."""
        paragraphs = re.split(r"\n\s*\n", text.strip())
        paragraphs = [p.strip() for p in paragraphs if p.strip()]
        if not paragraphs:
            return [], [], {}

        children: list[str] = []
        parents: list[str] = []
        child_to_parent: dict[int, int] = {}

        # Children = individual paragraphs (or sentence-level for oversized ones)
        for para in paragraphs:
            if len(para) <= self.chunk_size:
                children.append(para)
            else:
                # Oversized paragraph → split into sentences as children
                sentences = re.split(r"(?<=[。！？.!?\n])\s*", para)
                sentences = [s.strip() for s in sentences if s.strip()]
                for s in sentences:
                    if len(s) > self.chunk_size:
                        # Extreme fallback: fixed-size on a single sentence
                        for start in range(0, len(s), self.chunk_size):
                            children.append(s[start:start + self.chunk_size])
                    else:
                        children.append(s)

        # Parents = groups of paragraphs up to chunk_size
        current = ""
        current_child_start = 0
        for i, child in enumerate(children):
            if len(current) + len(child) > self.chunk_size and current:
                parents.append(current)
                for ci in range(current_child_start, i):
                    child_to_parent[ci] = len(parents) - 1
                current = child
                current_child_start = i
            else:
                current = current + "\n\n" + child if current else child
        if current:
            parents.append(current)
            for ci in range(current_child_start, len(children)):
                child_to_parent[ci] = len(parents) - 1

        return children, parents, child_to_parent

    # ------------------------------------------------------------------
    # original splitting methods
    # ------------------------------------------------------------------

    def _split(self, text: str) -> list[str]:
        if self.strategy == "fixed_size":
            return self._fixed_size_split(text)
        elif self.strategy == "sentence":
            return self._sentence_split(text)
        elif self.strategy == "markdown_header":
            return self._markdown_header_split(text)
        elif self.strategy == "recursive":
            return self._recursive_split(text)
        else:
            raise ValueError(f"Unknown chunking strategy: {self.strategy}")

    def _fixed_size_split(self, text: str) -> list[str]:
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunks.append(text[start:end])
            start += self.chunk_size - self.chunk_overlap
        return chunks

    def _sentence_split(self, text: str) -> list[str]:
        # Split on Chinese/English sentence boundaries
        sentences = re.split(r"(?<=[。！？.!?\n])\s*", text)
        sentences = [s.strip() for s in sentences if s.strip()]

        chunks = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) <= self.chunk_size:
                current += sent
            else:
                if current:
                    chunks.append(current)
                current = sent
        if current:
            chunks.append(current)
        return chunks

    def _markdown_header_split(self, text: str) -> list[str]:
        # Split on ## or ### headers
        sections = re.split(r"\n(?=#{2,3}\s)", text)
        return [s.strip() for s in sections if s.strip()]

    def _recursive_split(self, text: str) -> list[str]:
        """递归分块：段落 → 句子 → 字符滑动窗口，逐级降级。
        
        优先保持语义完整性，遇到超长文本自动降级到更细粒度。
        """
        # Level 1: 按段落切分（连续空行）
        paragraphs = re.split(r"\n\s*\n", text.strip())
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        result = []
        for para in paragraphs:
            if len(para) <= self.chunk_size:
                result.append(para)
                continue
            # Level 2: 段落太大，按句子切分
            sentences = re.split(r"(?<=[。！？.!?\n])\s*", para)
            sentences = [s.strip() for s in sentences if s.strip()]

            buffer = ""
            for sent in sentences:
                if len(sent) > self.chunk_size:
                    # Level 3: 句子太大，用字符滑动窗口
                    if buffer:
                        result.append(buffer)
                        buffer = ""
                    result.extend(self._fixed_size_split(sent))
                elif len(buffer) + len(sent) <= self.chunk_size:
                    buffer += sent
                else:
                    result.append(buffer)
                    buffer = sent
            if buffer:
                result.append(buffer)
        return result
