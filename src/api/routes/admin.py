"""权限与多租户：管理员后台接口 — 租户管理、用户管理、系统设置"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import get_pg_connection
from src.api.permissions import require_permission, can_manage_tenant

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────

class TenantCreateRequest(BaseModel):
    name: str
    code: str


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class UserRoleUpdateRequest(BaseModel):
    user_id: int
    role: str  # viewer / editor / tenant_admin


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: str


# ── Tenant Management ───────────────────────────────

@router.get("/tenants")
async def list_tenants(user: dict = Depends(require_permission("system:settings:view"))):
    """权限与多租户：获取所有租户列表（仅 super_admin）"""
    if user.get("role") != "super_admin":
        # tenant_admin 只能看自己的租户
        conn = get_pg_connection()
        try:
            row = conn.execute(
                "SELECT id, name, code, is_active, created_at FROM t_tenant WHERE id = %s",
                [user.get("tenant_id")],
            ).fetchone()
            return {"tenants": [{
                "id": row[0], "name": row[1], "code": row[2],
                "is_active": row[3], "created_at": row[4].isoformat() if row[4] else "",
            }] if row else []}
        finally:
            conn.close()

    conn = get_pg_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, code, is_active, created_at FROM t_tenant ORDER BY id"
        ).fetchall()
        return {"tenants": [{
            "id": r[0], "name": r[1], "code": r[2],
            "is_active": r[3], "created_at": r[4].isoformat() if r[4] else "",
        } for r in rows]}
    finally:
        conn.close()


@router.post("/tenants")
async def create_tenant(req: TenantCreateRequest, user: dict = Depends(require_permission("tenant:manage"))):
    """权限与多租户：创建新租户"""
    conn = get_pg_connection()
    try:
        conn.execute(
            "INSERT INTO t_tenant (name, code) VALUES (%s, %s)",
            [req.name, req.code],
        )
        conn.commit()
        return {"status": "ok", "message": f"租户 {req.name} 创建成功"}
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, "租户编码已存在")
        raise HTTPException(500, "创建租户失败")
    finally:
        conn.close()


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, req: TenantUpdateRequest, user: dict = Depends(require_permission("tenant:manage"))):
    """权限与多租户：更新租户信息"""
    if not can_manage_tenant(user, tenant_id):
        raise HTTPException(403, "无权操作此租户")

    conn = get_pg_connection()
    try:
        updates = []
        params = []
        if req.name is not None:
            updates.append("name = %s")
            params.append(req.name)
        if req.is_active is not None:
            updates.append("is_active = %s")
            params.append(req.is_active)
        if not updates:
            return {"status": "ok", "message": "无变更"}
        params.append(tenant_id)
        conn.execute(
            f"UPDATE t_tenant SET {', '.join(updates)} WHERE id = %s",
            params,
        )
        conn.commit()
        return {"status": "ok", "message": "租户更新成功"}
    finally:
        conn.close()


# ── User Management ─────────────────────────────────

@router.get("/users")
async def list_users(user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：获取用户列表，super_admin 看全部，其他角色看本租户"""
    conn = get_pg_connection()
    try:
        if user.get("role") == "super_admin":
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at,
                          u.tenant_id, t.name
                   FROM t_user u LEFT JOIN t_tenant t ON u.tenant_id = t.id
                   ORDER BY u.id"""
            ).fetchall()
        else:
            tenant_id = user.get("tenant_id")
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at,
                          u.tenant_id, t.name
                   FROM t_user u LEFT JOIN t_tenant t ON u.tenant_id = t.id
                   WHERE u.tenant_id = %s ORDER BY u.id""",
                [tenant_id],
            ).fetchall()
        return {"users": [{
            "id": r[0], "username": r[1], "display_name": r[2],
            "role": r[3], "is_active": r[4],
            "created_at": r[5].isoformat() if r[5] else "",
            "tenant_id": r[6], "tenant_name": r[7] or "",
        } for r in rows]}
    finally:
        conn.close()


@router.patch("/users/{target_user_id}/role")
async def update_user_role(target_user_id: int, req: UserRoleUpdateRequest, user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：修改用户角色"""
    if req.role not in ("viewer", "editor", "tenant_admin"):
        raise HTTPException(400, "无效角色，可选: viewer / editor / tenant_admin")

    tenant_id = user.get("tenant_id")
    conn = get_pg_connection()
    try:
        # 确认用户属于同一租户
        target = conn.execute(
            "SELECT id FROM t_user WHERE id = %s AND tenant_id = %s",
            [target_user_id, tenant_id],
        ).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在或不属于当前租户")

        conn.execute(
            "UPDATE t_user SET role = %s WHERE id = %s",
            [req.role, target_user_id],
        )
        conn.commit()
        return {"status": "ok", "message": f"用户角色已更新为 {req.role}"}
    finally:
        conn.close()


@router.patch("/users/{target_user_id}/toggle-active")
async def toggle_user_active(target_user_id: int, user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：启用/禁用用户"""
    tenant_id = user.get("tenant_id")
    conn = get_pg_connection()
    try:
        target = conn.execute(
            "SELECT id, is_active FROM t_user WHERE id = %s AND tenant_id = %s",
            [target_user_id, tenant_id],
        ).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在或不属于当前租户")

        new_status = not target[1]
        conn.execute(
            "UPDATE t_user SET is_active = %s WHERE id = %s",
            [new_status, target_user_id],
        )
        conn.commit()
        return {"status": "ok", "is_active": new_status}
    finally:
        conn.close()


# ── System Settings ─────────────────────────────────

@router.get("/settings")
async def get_settings(user: dict = Depends(require_permission("system:settings:view"))):
    """权限与多租户：获取系统设置（脱敏）"""
    from src.config import settings as app_settings
    return {
        "llm_provider": app_settings.llm_provider,
        "embedding_provider": app_settings.embedding_provider,
        "quality_guard_enabled": app_settings.quality_guard_enabled,
        "retrieval_mode": app_settings.retrieval_mode,
        "chunk_strategy": app_settings.chunk_strategy,
        "rerank_enabled": app_settings.rerank_enabled,
    }
