import logging

import httpx

from src.config import settings
from src.llm.base import BaseLLMProvider, ChatResult, StreamChunk

logger = logging.getLogger(__name__)


class ZhipuProvider(BaseLLMProvider):
    """Zhipu (智谱 GLM) provider via the zai SDK."""

    def __init__(self, api_key: str | None = None, default_model: str | None = None):
        self.api_key = api_key or settings.zhipu_api_key
        self.default_model = default_model or settings.zhipu_model
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from zai import ZhipuAiClient
            http_client = httpx.Client(verify=settings.ssl_verify) if not settings.ssl_verify else None
            self._client = ZhipuAiClient(api_key=self.api_key, http_client=http_client)
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
        if hasattr(response, 'usage') and response.usage:
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
        )
        for chunk in response:
            text = chunk.choices[0].delta.content if chunk.choices else None
            if text:
                yield StreamChunk(text=text)
