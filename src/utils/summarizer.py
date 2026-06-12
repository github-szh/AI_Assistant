"""Session message summarization — compress old messages into a rolling summary."""

import logging

from src.api.deps import get_pg_connection_sync
from src.config import settings
from src.llm.router import get_llm

logger = logging.getLogger(__name__)


def get_summary(session_id: str) -> str | None:
    """Fetch the current rolling summary for a session, best-effort."""
    try:
        with get_pg_connection_sync() as conn:
            row = conn.execute(
                "SELECT summary FROM t_session_info WHERE id=%s", [session_id]
            ).fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def summarize_session(session_id: str) -> None:
    """Check trigger threshold and generate/update the rolling summary.

    Called after every message save. Only acts when unsummarized user-message
    count exceeds chat_summarize_trigger.
    """
    trigger = settings.chat_summarize_trigger
    keep = settings.chat_summarize_keep_recent
    if trigger <= 0:
        return

    try:
        # Step 1: read messages from DB (briefly hold connection)
        with get_pg_connection_sync() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM t_session_message
                   WHERE session_id=%s AND role='user' AND (summarized IS NULL OR summarized=false)""",
                [session_id],
            ).fetchone()
            unsummarized = row[0] if row else 0

            if unsummarized <= trigger:
                return

            take = unsummarized - keep
            if take <= 0:
                return

            msgs = conn.execute(
                """SELECT id, role, content FROM t_session_message
                   WHERE session_id=%s AND (summarized IS NULL OR summarized=false)
                   ORDER BY id ASC LIMIT %s""",
                [session_id, take],
            ).fetchall()

            if not msgs:
                return

            lines = []
            for r in msgs:
                role_label = "用户" if r[1] == "user" else "AI"
                lines.append(f"{role_label}: {r[2][:2000]}")
            new_text = "\n".join(lines)

            existing = conn.execute(
                "SELECT summary FROM t_session_info WHERE id=%s", [session_id]
            ).fetchone()
            existing_summary = existing[0] if existing and existing[0] else ""

        # 连接已归还——LLM 调用不占用连接

        # Step 2: call LLM (slow, no connection held)
        summary = _generate_summary(existing_summary, new_text)

        # Step 3: save result (briefly hold connection again)
        if summary:
            with get_pg_connection_sync() as conn:
                conn.execute(
                    "UPDATE t_session_info SET summary=%s, updated_at=NOW() WHERE id=%s",
                    [summary, session_id],
                )
                msg_ids = [r[0] for r in msgs]
                conn.execute(
                    "UPDATE t_session_message SET summarized=true WHERE id = ANY(%s)",
                    [msg_ids],
                )
                conn.commit()
                logger.info("Summarized %d messages for session %s", len(msgs), session_id)
        else:
            logger.warning("Summary generation returned empty for session %s", session_id)

    except Exception as exc:
        logger.warning("summarize_session failed for %s: %s", session_id, exc)


def _generate_summary(existing: str, new_text: str) -> str:
    """Call LLM to produce an incremental conversation summary."""
    try:
        prompt_parts = ["你是一个对话摘要助手。请用 300 字以内总结以下对话的关键信息。"]

        if existing:
            prompt_parts.append(f"\n[已有摘要]\n{existing}")
            prompt_parts.append(f"\n[新增对话]\n{new_text}")
            prompt_parts.append("\n请将已有摘要和新增对话合并为一段新的摘要（300 字以内），保留关键脉络。")
        else:
            prompt_parts.append(f"\n[对话内容]\n{new_text}")
            prompt_parts.append("\n请生成一段摘要（300 字以内），保留关键脉络。")

        llm = get_llm()
        return llm.chat(
            messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
            temperature=0.0,
            max_tokens=600,
        )
    except Exception as exc:
        logger.warning("Summary LLM call failed: %s", exc)
        # Fallback: simple truncation of the new text
        return existing + "\n[早期对话]\n" + new_text[:500] if existing else new_text[:500]
