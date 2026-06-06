"""POST /chat/stream — SSE streaming via LLMRouter with fallback chain."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api.deps import get_pg_connection
from src.api.schemas import ChatRequest
from src.api.permissions import require_permission
from src.config import settings
from src.llm.router import get_llm
from src.utils.trim_messages import trim_messages
from src.utils.summarizer import get_summary, summarize_session

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
                summary = await asyncio.to_thread(get_summary, req.session_id)
                if summary:
                    system_text = f"[对话历史摘要]\n{summary}"
                    if msgs and msgs[0].get("role") == "system":
                        msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n\n" + system_text}
                    else:
                        msgs.insert(0, {"role": "system", "content": system_text})
            msgs = trim_messages(msgs, settings.chat_context_tokens, settings.chat_max_rounds)
            full_answer = ""
            for chunk in router.chat_stream(
                messages=msgs,
                provider=provider,
                model=req.model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                full_answer += chunk
                yield f"data: {json.dumps({'c': chunk})}\n\n"

            last_question = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_question = m["content"]
                    break
            logger.info("CHAT_STREAM %s", json.dumps({
                "question": last_question,
                "answer": full_answer,
            }, ensure_ascii=False))

            # 持久化消息（替代 /chat 的重复调用，省一次 LLM）
            if req.session_id and msgs:
                try:
                    async with get_pg_connection() as conn:
                        conn.execute(
                            "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                            [req.session_id, "user", last_question[:10000]],
                        )
                        conn.execute(
                            "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                            [req.session_id, "assistant", full_answer[:10000]],
                        )
                        conn.execute(
                            "UPDATE t_session_info SET updated_at=NOW() WHERE id=%s",
                            [req.session_id],
                        )
                        conn.commit()
                except Exception as exc:
                    logger.warning("Stream persist failed: %s", exc)

                # 自动摘要（best-effort）
                try:
                    asyncio.create_task(asyncio.to_thread(summarize_session, req.session_id))
                except Exception:
                    pass

            yield f"data: {json.dumps({'c': '', 'done': True})}\n\n"

        except Exception as e:
            logger.exception("Stream error: %s", provider)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
