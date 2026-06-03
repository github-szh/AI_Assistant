import logging

from openai import OpenAI

from src.config import settings
from src.llm.base import BaseLLMProvider, ChatResult, StreamChunk
from src.utils.ssl_utils import get_httpx_client

logger = logging.getLogger(__name__)


class AliProvider(BaseLLMProvider):
    """Alibaba Cloud DashScope LLM provider (OpenAI-compatible)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ):
        self.api_key = api_key or settings.ali_api_key
        self.base_url = base_url or settings.ali_base_url
        self.default_model = default_model or settings.ali_chat_model
        self._client: OpenAI | None = None

    def _ensure_client(self) -> OpenAI:
        if self._client is None:
            http_client = get_httpx_client()
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client)
        return self._client

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> ChatResult:
        if stream:
            chunks = list(self.chat_stream(
                messages, model=model, temperature=temperature, max_tokens=max_tokens,
            ))
            text = "".join(c.text for c in chunks if c.text)
            usage = next((c.usage for c in chunks if c.usage), None)
            return ChatResult(text=text, usage=usage)
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=model or self.default_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        text = response.choices[0].message.content or ""
        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return ChatResult(text=text, usage=usage)

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=model or self.default_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in response:
            text = chunk.choices[0].delta.content if chunk.choices else None
            if text:
                yield StreamChunk(text=text)
            if hasattr(chunk, 'usage') and chunk.usage:
                yield StreamChunk(usage={
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                })
