"""Session management — PG-backed CRUD."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends

from src.api.routes.auth import get_current_user
from src.api.permissions import require_permission
from src.api.deps import get_pg_connection
from src.config import settings

router = APIRouter(prefix="/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)


@router.post("")
async def create_session(user: dict = Depends(require_permission("chat:send"))):
    # 权限与多租户：创建会话时写入 tenant_id
    tenant_id = user.get("tenant_id")
    sid = uuid.uuid4().hex[:16]
    async with get_pg_connection() as conn:
        conn.execute(
            "INSERT INTO t_session_info (id, title, user_id, tenant_id, created_at, updated_at) VALUES (%s,%s,%s,%s,NOW(),NOW())",
            [sid, "新对话", user["user_id"], tenant_id],
        )
        conn.commit()
    return {"id": sid, "title": "新对话", "messages": []}


@router.get("")
async def list_sessions(user: dict = Depends(require_permission("chat:view"))):
    # 权限与多租户：super_admin 跳过租户过滤
    is_super = user.get("role") == "super_admin"
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        if is_super:
            rows = conn.execute(
                """SELECT s.id, s.title, s.created_at, s.updated_at,
                          (SELECT count(*) FROM t_session_message m WHERE m.session_id=s.id) as msg_count
                   FROM t_session_info s WHERE s.user_id=%s
                   ORDER BY COALESCE(s.updated_at, s.created_at) DESC""",
                [user["user_id"]],
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT s.id, s.title, s.created_at, s.updated_at,
                          (SELECT count(*) FROM t_session_message m WHERE m.session_id=s.id) as msg_count
                   FROM t_session_info s WHERE s.user_id=%s AND s.tenant_id=%s
                   ORDER BY COALESCE(s.updated_at, s.created_at) DESC""",
                [user["user_id"], tenant_id],
            ).fetchall()
    sessions = []
    for r in rows:
        sessions.append({
            "id": r[0], "title": r[1], "created_at": r[2].isoformat(),
            "updated_at": r[3].isoformat() if r[3] else "", "message_count": r[4],
        })
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/{sid}")
async def get_session(
    sid: str,
    limit: int = settings.chat_page_size,
    before_id: int | None = None,
    user: dict = Depends(require_permission("chat:view")),
):
    # 权限与多租户：校验会话归属
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        row = conn.execute(
            "SELECT id, title, user_id, summary FROM t_session_info WHERE id=%s AND tenant_id=%s",
            [sid, tenant_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, "会话不存在")
        if row[2] != user["user_id"]:
            raise HTTPException(403, "无权访问")

        fetch_limit = limit + 1
        if before_id:
            msgs = conn.execute(
                """SELECT id, role, content FROM (
                       SELECT id, role, content FROM t_session_message
                       WHERE session_id=%s AND id < %s
                       ORDER BY id DESC LIMIT %s
                   ) t ORDER BY id ASC""",
                [sid, before_id, fetch_limit],
            ).fetchall()
        else:
            msgs = conn.execute(
                """SELECT id, role, content FROM (
                       SELECT id, role, content FROM t_session_message
                       WHERE session_id=%s
                       ORDER BY id DESC LIMIT %s
                   ) t ORDER BY id ASC""",
                [sid, fetch_limit],
            ).fetchall()

    has_more = len(msgs) > limit
    if has_more:
        msgs = msgs[1:]

    return {
        "id": row[0], "title": row[1],
        "messages": [{"id": r[0], "role": r[1], "content": r[2]} for r in msgs],
        "has_more": has_more,
        "summary": row[3] or "",
    }


@router.patch("/{sid}")
async def rename_session(sid: str, title: str, user: dict = Depends(require_permission("chat:send"))):
    # 权限与多租户：校验租户
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        result = conn.execute(
            "UPDATE t_session_info SET title=%s, updated_at=NOW() WHERE id=%s AND user_id=%s AND tenant_id=%s",
            [title[:100], sid, user["user_id"], tenant_id],
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "会话不存在")
    return {"status": "ok"}


@router.delete("/{sid}")
async def delete_session(sid: str, user: dict = Depends(require_permission("chat:send"))):
    # 权限与多租户：校验租户
    tenant_id = user.get("tenant_id")
    async with get_pg_connection() as conn:
        result = conn.execute(
            "DELETE FROM t_session_info WHERE id=%s AND user_id=%s AND tenant_id=%s",
            [sid, user["user_id"], tenant_id],
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "会话不存在")
    return {"status": "deleted"}
