"""权限与多租户：角色定义、权限矩阵、校验依赖

从 t_role / t_permission / t_role_permission 表加载角色-权限映射，
首次调用时从数据库加载并缓存到内存。
"""

from fastapi import HTTPException, Depends
from src.api.routes.auth import get_current_user
from src.config import settings

# 内存缓存：role_name → [permission_code, ...]
_ROLE_PERMISSIONS_CACHE: dict[str, list[str]] | None = None


def _load_role_permissions() -> dict[str, list[str]]:
    """从数据库加载角色-权限映射"""
    import psycopg
    conn = psycopg.connect(
        host=settings.pg_host, port=settings.pg_port,
        dbname=settings.pg_database, user=settings.pg_user,
        password=settings.pg_password, connect_timeout=5,
    )
    try:
        roles = conn.execute("SELECT id, name FROM t_role").fetchall()
        perms = conn.execute("SELECT id, code FROM t_permission").fetchall()
        rp = conn.execute(
            "SELECT role_id, permission_id FROM t_role_permission"
        ).fetchall()
    finally:
        conn.close()

    perm_map = {pid: code for pid, code in perms}
    result: dict[str, list[str]] = {}
    # 按 role_id 分组
    rp_map: dict[int, list[int]] = {}
    for rid, pid in rp:
        rp_map.setdefault(rid, []).append(pid)

    for rid, rname in roles:
        codes = [perm_map[pid] for pid in rp_map.get(rid, [])]
        result[rname] = codes

    return result


def _get_permissions() -> dict[str, list[str]]:
    global _ROLE_PERMISSIONS_CACHE
    if _ROLE_PERMISSIONS_CACHE is None:
        _ROLE_PERMISSIONS_CACHE = _load_role_permissions()
    return _ROLE_PERMISSIONS_CACHE


def check_permission(role: str, required: str) -> bool:
    """检查角色是否拥有指定权限"""
    perms = _get_permissions().get(role, [])
    return "*" in perms or required in perms


def require_permission(permission: str):
    """FastAPI Depends 兼容的权限校验

    用法:
        @router.get("/documents")
        async def list_documents(user: dict = Depends(require_permission("document:view"))):
            ...
    """
    def permission_dependency(user: dict = Depends(get_current_user)):
        if not check_permission(user.get("role", "viewer"), permission):
            raise HTTPException(403, f"权限不足，需要 {permission} 权限")
        return user
    return permission_dependency


def can_manage_tenant(user: dict, target_tenant_id: int) -> bool:
    """判断用户是否有权操作指定租户的数据"""
    role = user.get("role", "viewer")
    if role == "super_admin":
        return True
    return user.get("tenant_id") == target_tenant_id
