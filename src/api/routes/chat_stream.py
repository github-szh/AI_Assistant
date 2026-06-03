"""POST /chat/stream — SSE streaming via LLMRouter with fallback chain."""

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.schemas import ChatRequest
from src.api.permissions import require_permission
from src.config import settings
from src.llm.router import get_llm
from src.utils.trim_messages import trim_messages
from src.utils.summarizer import get_summary

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(require_permission("chat:send"))):

    async def generate():
        provider = req.provider or "deepseek"
        router = get_llm()

        try:
            msgs = list(req.messages)
            if req.session_id:
                summary = get_summary(req.session_id)
                if summary:
                    system_text = f"[对话历史摘要]\n{summary}"
                    if msgs and msgs[0].get("role") == "system":
                        msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n\n" + system_text}
                    else:
                        msgs.insert(0, {"role": "system", "content": system_text})
            msgs = trim_messages(msgs, settings.chat_context_tokens, settings.chat_max_rounds)
            for chunk in router.chat_stream(
                messages=msgs,
                provider=provider,
                model=req.model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                yield f"data: {json.dumps({'c': chunk})}\n\n"

            yield f"data: {json.dumps({'c': '', 'done': True})}\n\n"

        except Exception as e:
            logger.exception("Stream error: %s", provider)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
