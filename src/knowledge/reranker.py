"""RAG reranker via bge-reranker-v2-m3 — subprocess-isolated.

FlagReranker (FlagEmbedding) and llama_index import conflicting HuggingFace
tokenizer backends. Loading both in the same process causes a segfault on
inference. The fix: run the reranker in a clean subprocess that never
imports llama_index, communicating via stdin/stdout JSON lines.

Protocol:
  IN:  {"query": "...", "candidates": ["...", ...], "top_k": 5, "min_score": null}
  OUT: {"results": [["text", 0.95], ...]} or {"error": "..."}
"""

import atexit
import json
import logging
import subprocess
import sys
from functools import lru_cache

from src.config import settings

logger = logging.getLogger(__name__)

_WORKER_SCRIPT = __file__.rsplit("src", 1)[0] + "src/knowledge/reranker_worker.py"


class Reranker:
    """Re-rank results via bge-reranker-v2-m3 running in a subprocess.

    The subprocess is spawned lazily on first use and kept alive for
    subsequent calls, avoiding repeated model-loading overhead.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def _ensure_worker(self):
        """Spawn the reranker worker subprocess if not already running."""
        if self._proc is not None and self._proc.poll() is not None:
            # Worker died — clean up and restart
            self._proc = None

        if self._proc is None:
            logger.info("Starting reranker subprocess: %s", _WORKER_SCRIPT)
            self._proc = subprocess.Popen(
                [sys.executable, "-u", _WORKER_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

    def rerank(
        self,
        query: str,
        candidates: list[str],
        top_k: int = 5,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        """Re-rank candidates by relevance to query (via subprocess)."""
        if not candidates:
            return []

        self._ensure_worker()

        request = json.dumps({
            "query": query,
            "candidates": candidates,
            "top_k": top_k,
            "min_score": min_score,
        })

        try:
            self._proc.stdin.write(request + "\n")
            self._proc.stdin.flush()
            response_line = self._proc.stdout.readline()

            if not response_line:
                raise RuntimeError("Reranker worker closed stdout unexpectedly")

            response = json.loads(response_line)

            if "error" in response:
                raise RuntimeError(response["error"])

            return [(item[0], item[1]) for item in response.get("results", [])]

        except (BrokenPipeError, OSError, RuntimeError) as exc:
            logger.warning("Reranker worker failed: %s, falling back to raw order", exc)
            # Clean up dead worker so next call re-spawns
            self._close_worker()
            return list(zip(candidates, [0.0] * len(candidates)))[:top_k]

    def _close_worker(self):
        """Terminate the worker subprocess."""
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.stdout.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def close(self):
        """Public cleanup method."""
        self._close_worker()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    """Singleton reranker instance."""
    return Reranker()


@atexit.register
def _cleanup():
    """Kill the reranker worker on interpreter exit."""
    r = get_reranker()
    r.close()
