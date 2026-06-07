"""权限与多租户：管理员后台接口 — 租户管理、用户管理、系统设置"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import bcrypt

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


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "viewer"
    tenant_id: int | None = None


class UserRoleUpdateRequest(BaseModel):
    user_id: int
    role: str


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
        async with get_pg_connection() as conn:
            row = conn.execute(
                "SELECT id, name, code, is_active, created_at FROM t_tenant WHERE id = %s",
                [user.get("tenant_id")],
            ).fetchone()
            return {"tenants": [{
                "id": row[0], "name": row[1], "code": row[2],
                "is_active": row[3], "created_at": row[4].isoformat() if row[4] else "",
            }] if row else []}

    async with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, code, is_active, created_at FROM t_tenant ORDER BY id"
        ).fetchall()
        return {"tenants": [{
            "id": r[0], "name": r[1], "code": r[2],
            "is_active": r[3], "created_at": r[4].isoformat() if r[4] else "",
        } for r in rows]}


@router.post("/tenants")
async def create_tenant(req: TenantCreateRequest, user: dict = Depends(require_permission("tenant:manage"))):
    """权限与多租户：创建新租户"""
    try:
        async with get_pg_connection() as conn:
            conn.execute(
                "INSERT INTO t_tenant (name, code) VALUES (%s, %s)",
                [req.name, req.code],
            )
            conn.commit()
            logger.info("租户已创建 (name=%s, code=%s, 操作人=%s)", req.name, req.code, user.get("username"))
            return {"status": "ok", "message": f"租户 {req.name} 创建成功"}
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, "租户编码已存在")
        raise HTTPException(500, "创建租户失败")


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, req: TenantUpdateRequest, user: dict = Depends(require_permission("tenant:manage"))):
    """权限与多租户：更新租户信息"""
    if not can_manage_tenant(user, tenant_id):
        raise HTTPException(403, "无权操作此租户")

    async with get_pg_connection() as conn:
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
        logger.info("租户已更新 (tenant_id=%s, 操作人=%s)", tenant_id, user.get("username"))
        return {"status": "ok", "message": "租户更新成功"}


# ── User Management ─────────────────────────────────

@router.get("/users")
async def list_users(user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：获取用户列表"""
    async with get_pg_connection() as conn:
        if user.get("role") == "super_admin":
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at,
                          u.tenant_id, t.name
                   FROM t_user u LEFT JOIN t_tenant t ON u.tenant_id = t.id
                   ORDER BY CASE u.role WHEN 'super_admin' THEN 1 WHEN 'tenant_admin' THEN 2 WHEN 'editor' THEN 3 WHEN 'viewer' THEN 4 END, u.id"""
            ).fetchall()
        else:
            tenant_id = user.get("tenant_id")
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at,
                          u.tenant_id, t.name
                   FROM t_user u LEFT JOIN t_tenant t ON u.tenant_id = t.id
                   WHERE u.tenant_id = %s AND u.role != 'super_admin'
                   ORDER BY CASE u.role WHEN 'super_admin' THEN 1 WHEN 'tenant_admin' THEN 2 WHEN 'editor' THEN 3 WHEN 'viewer' THEN 4 END, u.id""",
                [tenant_id],
            ).fetchall()
        return {"users": [{
            "id": r[0], "username": r[1], "display_name": r[2],
            "role": r[3], "is_active": r[4],
            "created_at": r[5].isoformat() if r[5] else "",
            "tenant_id": r[6], "tenant_name": r[7] or "",
        } for r in rows]}


@router.post("/users")
async def create_user(req: CreateUserRequest, user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：创建新用户"""
    valid_roles = ("viewer", "editor", "tenant_admin", "super_admin")
    if req.role not in valid_roles:
        raise HTTPException(400, f"无效角色，可选: viewer / editor / tenant_admin / super_admin")

    # 只有超级管理员能创建管理员（含 super_admin 和 tenant_admin）
    if req.role in ("super_admin", "tenant_admin") and user.get("role") != "super_admin":
        raise HTTPException(403, "只有超级管理员才能创建管理员")

    if user.get("role") == "super_admin":
        if not req.tenant_id:
            raise HTTPException(400, "超级管理员创建用户时必须指定租户")
        tenant_id = req.tenant_id
    else:
        tenant_id = user.get("tenant_id")
        if not tenant_id:
            raise HTTPException(400, "无法确定目标租户")

    if len(req.username) < 2 or len(req.password) < 4:
        raise HTTPException(400, "用户名至少2位，密码至少4位")

    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    try:
        async with get_pg_connection() as conn:
            conn.execute(
                """INSERT INTO t_user (username, password_hash, display_name, role, is_active, tenant_id)
                   VALUES (%s, %s, %s, %s, TRUE, %s)""",
                [req.username, pw_hash, req.display_name or req.username, req.role, tenant_id],
            )
            conn.commit()
            logger.info("用户 %s 已创建 (角色=%s, 租户=%s, 操作用户=%s)",
                        req.username, req.role, tenant_id, user.get("username"))
            return {"status": "ok", "message": f"用户 {req.username} 创建成功"}
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(409, "用户名已存在")
        raise HTTPException(500, f"创建用户失败: {e}")


@router.patch("/users/{target_user_id}/role")
async def update_user_role(target_user_id: int, req: UserRoleUpdateRequest, user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：修改用户角色"""
    valid_roles = ("viewer", "editor", "tenant_admin", "super_admin")
    if req.role not in valid_roles:
        raise HTTPException(400, "无效角色，可选: viewer / editor / tenant_admin / super_admin")

    if req.role == "super_admin" and user.get("role") != "super_admin":
        raise HTTPException(403, "无权赋予超级管理员角色")

    role_hierarchy = {"viewer": 0, "editor": 1, "tenant_admin": 2, "super_admin": 3}

    async with get_pg_connection() as conn:
        target = conn.execute(
            "SELECT id, tenant_id, role FROM t_user WHERE id = %s",
            [target_user_id],
        ).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")

        target_role = target[2]
        operator_role = user.get("role", "viewer")

        # 非 super_admin 不能操作 super_admin
        if target_role == "super_admin" and operator_role != "super_admin":
            raise HTTPException(403, "无权修改超级管理员的角色")

        # 只能操作比自己角色低的用户（super_admin 除外，可操作所有人）
        if operator_role != "super_admin":
            if role_hierarchy.get(target_role, -1) >= role_hierarchy.get(operator_role, -1):
                raise HTTPException(403, "无权修改同级或上级用户")
            if role_hierarchy.get(req.role, -1) >= role_hierarchy.get(operator_role, -1):
                raise HTTPException(403, "无权赋予同级或上级权限")

        # 租户隔离
        if operator_role != "super_admin" and target[1] != user.get("tenant_id"):
            raise HTTPException(403, "无权操作其他租户的用户")

        conn.execute(
            "UPDATE t_user SET role = %s WHERE id = %s",
            [req.role, target_user_id],
        )
        conn.commit()
        logger.info("用户角色已更新 (user_id=%s, %s → %s, 操作人=%s)", target_user_id, target_role, req.role, user.get("username"))
        return {"status": "ok", "message": f"用户角色已更新为 {req.role}"}


@router.patch("/users/{target_user_id}/toggle-active")
async def toggle_user_active(target_user_id: int, user: dict = Depends(require_permission("tenant:users:manage"))):
    """权限与多租户：启用/禁用用户"""
    async with get_pg_connection() as conn:
        target = conn.execute(
            "SELECT id, tenant_id, is_active, role FROM t_user WHERE id = %s",
            [target_user_id],
        ).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")

        target_role = target[3]
        operator_role = user.get("role", "viewer")

        # 非 super_admin 不能操作 super_admin
        if target_role == "super_admin" and operator_role != "super_admin":
            raise HTTPException(403, "无权操作超级管理员用户")

        if operator_role != "super_admin" and target[1] != user.get("tenant_id"):
            raise HTTPException(403, "无权操作其他租户的用户")

        new_status = not target[2]
        conn.execute(
            "UPDATE t_user SET is_active = %s WHERE id = %s",
            [new_status, target_user_id],
        )
        conn.commit()
        logger.info("用户状态已切换 (user_id=%s, is_active=%s, 操作人=%s)", target_user_id, new_status, user.get("username"))
        return {"status": "ok", "is_active": new_status}


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
