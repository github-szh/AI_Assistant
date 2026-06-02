"""Reranker subprocess worker — isolates FlagReranker from llama_index.

Runs in a clean Python process that never imports llama_index, avoiding
the HuggingFace tokenizer conflict that causes segfaults when both
llama_index and FlagEmbedding coexist in the same process.

Protocol (stdin/stdout JSON, one line per request):
  IN:  {"query": "...", "candidates": ["...", ...], "top_k": 5, "min_score": null}
  OUT: {"results": [["text", 0.95], ...]} or {"error": "..."}
"""

import json
import os as _os
import sys

_PROJECT_ROOT = __file__.rsplit("src", 1)[0]
_RERANKER_LOCAL = _os.path.join(_PROJECT_ROOT, "data", "models", "BAAI", "bge-reranker-v2-m3")
_RERANKER_DEFAULT = "BAAI/bge-reranker-v2-m3"
_RERANKER_MODEL = _RERANKER_LOCAL if _os.path.isdir(_RERANKER_LOCAL) else _RERANKER_DEFAULT


def main():
    # Warm up the model once
    from FlagEmbedding import FlagReranker
    import logging
    logging.basicConfig(level=logging.WARNING)

    model = FlagReranker(_RERANKER_MODEL, use_fp16=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        query = req.get("query", "")
        candidates = req.get("candidates", [])
        top_k = req.get("top_k", 5)
        min_score = req.get("min_score")

        if not candidates:
            sys.stdout.write(json.dumps({"results": []}) + "\n")
            sys.stdout.flush()
            continue

        try:
            pairs = [[query, c] for c in candidates]
            scores = model.compute_score(pairs)
            if isinstance(scores, float):
                scores = [scores]

            ranked = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)

            if min_score is not None:
                kept = [item for item in ranked if item[1] >= min_score]
                if kept:
                    ranked = kept

            if len(ranked) >= 2 and ranked[0][1] != ranked[-1][1]:
                min_s = ranked[-1][1]
                max_s = ranked[0][1]
                ranked = [(t, (s - min_s) / (max_s - min_s)) for t, s in ranked]

            ranked = ranked[:top_k]
            sys.stdout.write(json.dumps({"results": ranked}) + "\n")
            sys.stdout.flush()

        except Exception as exc:
            sys.stdout.write(json.dumps({"error": str(exc)}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
