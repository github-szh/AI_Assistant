"""Embedding — multi-provider support (Zhipu, Ali, local BGE)."""

import hashlib
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from src.config import settings
from src.monitoring.metrics import record_llm_call
from src.storage.cache import get_memory_cache
from src.utils.ssl_utils import get_verify_param, get_httpx_client

logger = logging.getLogger(__name__)


class _ZhipuAPI:
    """Zhipu embedding-3 API client."""

    def __init__(self):
        self.key = settings.zhipu_api_key
        self.model = settings.zhipu_embedding_model
        self.url = settings.zhipu_embedding_url
        self.dim = settings.embedding_dim
        self.batch = settings.embedding_batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        verify = get_verify_param()
        results = []
        start = time.time()
        total_input = 0
        for i in range(0, len(texts), self.batch):
            batch = texts[i:i + self.batch]
            total_input += sum(len(t) for t in batch)
            resp = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "input": batch, "dimensions": self.dim},
                timeout=120,
                verify=verify,
            )
            resp.raise_for_status()
            results.extend([d["embedding"] for d in resp.json()["data"]])
        elapsed = time.time() - start
        prompt_tokens = max(1, total_input * 2 // 5)
        record_llm_call("zhipu", self.model, prompt_tokens, 0, elapsed)
        return results


class _AliAPI:
    """Alibaba Cloud DashScope embedding client (OpenAI-compatible)."""

    def __init__(self):
        self.api_key = settings.ali_api_key
        self.model = settings.ali_embedding_model
        self.batch = settings.embedding_batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        http_client = get_httpx_client()
        client = OpenAI(api_key=self.api_key, base_url=settings.ali_base_url, http_client=http_client)
        results = []
        start = time.time()
        total_input = 0
        for i in range(0, len(texts), self.batch):
            batch = texts[i:i + self.batch]
            total_input += sum(len(t) for t in batch)
            resp = client.embeddings.create(
                model=self.model,
                input=batch,
            )
            results.extend([d.embedding for d in resp.data])
        elapsed = time.time() - start
        prompt_tokens = max(1, total_input * 2 // 5)
        record_llm_call("ali", self.model, prompt_tokens, 0, elapsed)
        return results


class _LocalBGE:
    """Local BGE-M3 embedding model (offline fallback)."""

    def __init__(self):
        self._model = None
        self.dim = 1024

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            model_path = Path(settings.models_cache_dir) / "BAAI" / "bge-m3"
            if model_path.exists():
                self._model = SentenceTransformer(str(model_path))
            else:
                self._model = SentenceTransformer("BAAI/bge-m3")
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()


class EmbeddingManager:
    """Singleton access to embedding provider (Zhipu or Ali)."""

    def __init__(self):
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        provider = settings.embedding_provider
        if provider == "ali":
            if settings.ali_api_key:
                logger.info("Using Ali %s (API)", settings.ali_embedding_model)
                self._model = _AliAPI()
                return self._model
            if settings.zhipu_api_key:
                logger.info("Ali key missing, fallback to Zhipu %s", settings.zhipu_embedding_model)
                self._model = _ZhipuAPI()
                return self._model
        else:
            if settings.zhipu_api_key:
                logger.info("Using Zhipu %s (API)", settings.zhipu_embedding_model)
                self._model = _ZhipuAPI()
                return self._model
            if settings.ali_api_key:
                logger.info("Zhipu key missing, fallback to Ali %s", settings.ali_embedding_model)
                self._model = _AliAPI()
                return self._model

        logger.info("No remote embedding key configured, using local bge-m3")
        self._model = _LocalBGE()
        return self._model

    @property
    def model(self):
        return self._ensure_model()

    def encode(self, texts: str | list[str]) -> list[list[float]]:
        single = isinstance(texts, str)
        texts_list = [texts] if single else texts
        return self._ensure_model().embed(texts_list)

    def encode_query(self, query: str) -> list[float]:
        cache = get_memory_cache()
        key = f"emb:{hashlib.md5(query.encode()).hexdigest()}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        emb = self._ensure_model().embed([query])[0]
        cache.set(key, emb, ttl=3600)
        return emb


@lru_cache(maxsize=1)
def get_embedding_manager() -> EmbeddingManager:
    return EmbeddingManager()
