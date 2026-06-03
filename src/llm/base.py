"""(Sync) abstract base for LLM providers."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class ChatResult:
    """Result of a non-streaming chat call."""
    text: str
    usage: dict | None = None  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}


@dataclass
class StreamChunk:
    """A single chunk from a streaming chat call.

    Carries either text content or (on the last chunk) token usage metadata.
    """
    text: str | None = None
    usage: dict | None = None


class BaseLLMProvider(ABC):
    """Every LLM backend implements this interface."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> ChatResult:
        """Send a chat completion request and return text + usage metadata."""
        ...

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Iterator[StreamChunk]:
        """Yield StreamChunks (text and optional usage) from a streaming completion.

        Default: call chat() with stream=True and yield the result as one chunk.
        Providers that support native streaming should override this.
        """
        result = self.chat(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        if result.text:
            yield StreamChunk(text=result.text)
        if result.usage:
            yield StreamChunk(usage=result.usage)

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether the provider is configured and reachable."""
        ...
