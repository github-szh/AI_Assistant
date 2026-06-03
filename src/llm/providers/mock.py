from src.llm.base import BaseLLMProvider, ChatResult


class MockProvider(BaseLLMProvider):
    """Returns deterministic responses for local testing without API calls."""

    def is_available(self) -> bool:
        return True

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> ChatResult:
        last_message = messages[-1]["content"] if messages else ""
        return ChatResult(text=f"[mock] received: {last_message}")
