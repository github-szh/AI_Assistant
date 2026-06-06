"""POST /chat — LLM conversation with auto session persistence."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends

from src.api.schemas import ChatRequest, ChatResponse
from src.api.permissions import require_permission
from src.api.deps import get_pg_connection
from src.config import settings
from src.llm.router import get_llm
from src.utils.trim_messages import trim_messages
from src.utils.summarizer import get_summary, summarize_session

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict = Depends(require_permission("chat:send"))):
    llm = get_llm()

    # Inject system prompt from YAML template
    from src.utils.prompt_loader import load_system_prompt
    system_prompt = load_system_prompt("assistant")
    messages = req.messages
    if system_prompt and (not messages or messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": system_prompt}] + list(messages)

    # Trim context to fit token budget and round limit
    messages = trim_messages(messages, settings.chat_context_tokens, settings.chat_max_rounds)

    # Inject session summary into system prompt
    if req.session_id:
        summary = await asyncio.to_thread(get_summary, req.session_id)
        if summary and messages and messages[0].get("role") == "system":
            messages[0]["content"] += "\n\n[对话历史摘要]\n" + summary

    # Detect RAG path: last message is already an assistant answer — skip LLM call
    last_msg = req.messages[-1] if req.messages else None
    if last_msg and last_msg.get("role") == "assistant":
        content = last_msg["content"]
    else:
        content = llm.chat(
            messages=messages,
            provider=req.provider,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )

    # Auto-persist messages if session_id provided
    if req.session_id and req.messages:
        try:
            async with get_pg_connection() as conn:
                # Save the last user message
                user_msgs = [m for m in req.messages if m.get("role") == "user"]
                user_msg = user_msgs[-1]["content"] if user_msgs else ""
                conn.execute(
                    "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                    [req.session_id, "user", user_msg[:10000]],
                )
                # Save assistant message
                conn.execute(
                    "INSERT INTO t_session_message (session_id, role, content) VALUES (%s,%s,%s)",
                    [req.session_id, "assistant", content[:10000]],
                )
                # Auto-name: use first 10 chars of the first USER message
                user_msgs = [m["content"] for m in req.messages if m.get("role") == "user"]
                first_text = user_msgs[0].strip().replace("\n", " ") if user_msgs else ""
                title = first_text[:10] if first_text else "新对话"
                conn.execute(
                    "UPDATE t_session_info SET title=%s, updated_at=NOW() WHERE id=%s AND title='新对话'",
                    [title, req.session_id],
                )
                conn.execute(
                    "UPDATE t_session_info SET updated_at=NOW() WHERE id=%s",
                    [req.session_id],
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to persist chat message: %s", exc)

    # Try summarization after save (best-effort, separate connection)
    if req.session_id:
        try:
            asyncio.create_task(asyncio.to_thread(summarize_session, req.session_id))
        except Exception:
            pass

    last_user_msg = ""
    for m in reversed(req.messages):
        if m.get("role") == "user":
            last_user_msg = m["content"]
            break
    logger.info("CHAT %s", json.dumps({
        "question": last_user_msg,
        "answer": content,
    }, ensure_ascii=False))
    return ChatResponse(
        content=content,
        provider=req.provider,
        model=req.model or settings.deepseek_model,
    )
