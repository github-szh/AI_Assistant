"""权限与多租户：角色定义、权限矩阵、校验依赖"""

from fastapi import HTTPException, Depends
from src.api.routes.auth import get_current_user

# 权限与多租户：角色-权限映射表
ROLE_PERMISSIONS = {
    "super_admin": ["*"],

    "tenant_admin": [
        "tenant:view", "tenant:manage", "tenant:users:manage",
        "document:upload", "document:view", "document:delete", "document:download",
        "chat:send", "chat:view", "chat:delete",
        "knowledge:query",
        "monitoring:view",
        "quality:view",
        "system:settings:view", "system:llm:switch",
    ],

    "editor": [
        "document:upload", "document:view", "document:delete", "document:download",
        "chat:send", "chat:view", "chat:delete",
        "knowledge:query",
        "quality:view",
    ],

    "viewer": [
        "chat:send", "chat:view", "chat:delete",
        "knowledge:query",
        "quality:view",
    ],
}


def check_permission(role: str, required: str) -> bool:
    """检查角色是否拥有指定权限"""
    perms = ROLE_PERMISSIONS.get(role, [])
    return "*" in perms or required in perms


def require_permission(permission: str):
    """权限与多租户：FastAPI Depends 兼容的权限校验

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
    """权限与多租户：判断用户是否有权操作指定租户的数据"""
    role = user.get("role", "viewer")
    if role == "super_admin":
        return True
    return user.get("tenant_id") == target_tenant_id
