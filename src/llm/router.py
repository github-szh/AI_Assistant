"""LLM Router — unified interface with provider selection, retry, and fallback."""

import logging
import time
from collections.abc import Iterator
from functools import lru_cache

from src.config import settings
from src.llm.base import BaseLLMProvider
from src.llm.providers.deepseek import DeepSeekProvider
from src.llm.providers.openai import OpenAIProvider
from src.llm.providers.zhipu import ZhipuProvider
from src.llm.providers.ali import AliProvider
from src.llm.providers.mock import MockProvider

logger = logging.getLogger(__name__)


def _usage_str(usage: dict | None) -> str:
    """Format token usage for logging. Returns '?' when unavailable."""
    if not usage:
        return "in:?/out:?/total:?"
    return (
        f"in:{usage.get('prompt_tokens', '?')}"
        f"/out:{usage.get('completion_tokens', '?')}"
        f"/total:{usage.get('total_tokens', '?')}"
    )

# Provider name constants
DEEPSEEK = "deepseek"
OPENAI = "openai"
ZHIPU = "zhipu"
ALI = "ali"
MOCK = "mock"

# Ordered list of providers to try when the primary fails
DEFAULT_FALLBACK_CHAIN = [DEEPSEEK, ZHIPU, OPENAI, MOCK]


class LLMRouter:
    """Routes chat requests to the appropriate LLM provider.

    Supports:
    - Explicit provider selection
    - Automatic retry with backoff
    - Fallback chain when primary provider fails
    """

    def __init__(self):
        self._providers: dict[str, BaseLLMProvider] = {}

    def register(self, name: str, provider: BaseLLMProvider) -> None:
        self._providers[name] = provider

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = False,
        retries: int = 2,
        backoff: float = 1.0,
        fallback_chain: list[str] | None = None,
    ) -> str:
        """Send chat request with retry and fallback.

        Args:
            messages: Chat messages in OpenAI format.
            provider: Primary provider name (defaults to settings.llm_provider).
            model: Model override (uses provider default if None).
            temperature: Sampling temperature.
            max_tokens: Max tokens in response.
            stream: Whether to stream (collects and returns full text).
            retries: Number of retry attempts per provider.
            backoff: Base seconds between retries.
            fallback_chain: Ordered backup providers if primary fails.
                Defaults to [deepseek, zhipu, openai, mock].

        Returns:
            Text response from the first successful provider.

        Raises:
            RuntimeError: If all providers in the chain fail.
        """
        provider = provider or settings.llm_provider
        chain = fallback_chain or DEFAULT_FALLBACK_CHAIN
        # Ensure primary provider is first in chain
        ordered = [provider] + [p for p in chain if p != provider]

        last_error: Exception | None = None

        for prov_name in ordered:
            prov = self._providers.get(prov_name)
            if prov is None or not prov.is_available():
                logger.debug("Provider %s not available, skipping", prov_name)
                continue

            for attempt in range(retries + 1):
                try:
                    t_start = time.monotonic()
                    result = prov.chat(
                        messages,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=stream,
                    )
                    elapsed = time.monotonic() - t_start
                    logger.info(
                        "LLM: provider=%s model=%s 耗时=%.2fs tokens=%s",
                        prov_name,
                        model or "default",
                        elapsed,
                        _usage_str(result.usage),
                    )
                    return result.text
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Provider %s attempt %d/%d failed: %s",
                        prov_name,
                        attempt + 1,
                        retries + 1,
                        exc,
                    )
                    if attempt < retries:
                        time.sleep(backoff * (attempt + 1))

            logger.error("Provider %s exhausted all retries", prov_name)

        raise RuntimeError(
            f"All providers failed. Last error: {last_error}"
        ) from last_error

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        fallback_chain: list[str] | None = None,
    ) -> Iterator[str]:
        """Stream chat with the same fallback chain as blocking chat.

        Yields text chunks from the first successful provider. If a provider
        fails before producing any chunks, the next provider in the chain
        is tried automatically.
        """
        provider = provider or settings.llm_provider
        chain = fallback_chain or DEFAULT_FALLBACK_CHAIN
        ordered = [provider] + [p for p in chain if p != provider]

        last_error: Exception | None = None

        for prov_name in ordered:
            prov = self._providers.get(prov_name)
            if prov is None or not prov.is_available():
                logger.debug("Provider %s not available, skipping", prov_name)
                continue

            try:
                t_start = time.monotonic()
                stream_usage = None

                for chunk in prov.chat_stream(
                    messages, model=model, temperature=temperature,
                    max_tokens=max_tokens,
                ):
                    if chunk.text:
                        yield chunk.text
                    if chunk.usage:
                        stream_usage = chunk.usage

                elapsed = time.monotonic() - t_start
                logger.info(
                    "LLM stream: provider=%s model=%s 耗时=%.2fs tokens=%s",
                    prov_name,
                    model or "default",
                    elapsed,
                    _usage_str(stream_usage),
                )
                return  # stream completed successfully
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Provider %s stream failed: %s, trying next in chain",
                    prov_name, exc,
                )

        logger.error("All providers failed for stream. Last error: %s", last_error)
        yield f"[错误] 所有AI服务暂时不可用，请稍后重试"


@lru_cache(maxsize=1)
def get_llm() -> LLMRouter:
    """Singleton LLM router with all providers registered."""
    router = LLMRouter()
    router.register(DEEPSEEK, DeepSeekProvider())
    router.register(ZHIPU, ZhipuProvider())
    router.register(ALI, AliProvider())
    router.register(OPENAI, OpenAIProvider())
    router.register(MOCK, MockProvider())
    return router
