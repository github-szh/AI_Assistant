"""JWT-based authentication — login + register."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
import bcrypt
import jwt

from src.config import settings
from src.api.deps import get_pg_connection

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

JWT_SECRET = settings.jwt_secret
JWT_EXPIRE_HOURS = 24


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    tenant_code: str = ""  # 权限与多租户：注册时指定租户编码，空则创建个人租户


class AuthResponse(BaseModel):
    token: str
    user_id: int
    username: str
    display_name: str
    role: str = "viewer"  # 权限与多租户：返回角色
    tenant_id: int | None = None  # 权限与多租户：返回租户ID
    tenant_name: str = ""  # 权限与多租户：返回租户名称


def create_jwt(user_id: int, username: str, role: str = "viewer", tenant_id: int | None = None) -> str:
    # 权限与多租户：JWT payload 增加 role 和 tenant_id
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "tenant_id": tenant_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_jwt(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


@router.post("/register", response_model=AuthResponse)
async def register(req: RegisterRequest, request: Request):
    # 限流：每 IP 每 60 秒最多 3 次注册
    from src.storage.cache import RateLimiter
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.is_allowed(f"register:{client_ip}"):
        logger.warning("注册限流触发: IP=%s, username=%s", client_ip, req.username)
        raise HTTPException(429, "注册过于频繁，请稍后再试")

    if len(req.username) < 2 or len(req.password) < 8:
        raise HTTPException(400, "用户名至少2位，密码至少8位")

    # 邀请制模式：不允许自动创建个人租户
    if settings.invite_only_registration and not req.tenant_code:
        raise HTTPException(400, "当前为邀请制注册，请提供租户编码")

    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()

    async with get_pg_connection() as conn:
        # 权限与多租户：注册时绑定租户
        tenant_id = None
        tenant_name = ""

        if req.tenant_code:
            # 加入已有租户
            row = conn.execute(
                "SELECT id, name FROM t_tenant WHERE code = %s AND is_active = TRUE",
                [req.tenant_code],
            ).fetchone()
            if not row:
                raise HTTPException(404, "租户不存在或已禁用")
            tenant_id = row[0]
            tenant_name = row[1]
        else:
            # 创建个人租户
            code = f"user_{uuid.uuid4().hex[:8]}"
            name = f"{req.username}的个人空间"
            conn.execute(
                "INSERT INTO t_tenant (name, code) VALUES (%s, %s)",
                [name, code],
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, name FROM t_tenant WHERE code = %s", [code],
            ).fetchone()
            tenant_id = row[0]
            tenant_name = row[1]

        # 注册用户，默认角色为 viewer
        try:
            row = conn.execute(
                """INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
                   VALUES (%s, %s, %s, 'viewer', %s) RETURNING id""",
                [req.username, pw_hash, req.display_name or req.username, tenant_id],
            ).fetchone()
            conn.commit()
            user_id = row[0]
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(409, "用户名已存在")
            raise HTTPException(500, "注册失败")

    token = create_jwt(user_id, req.username, "viewer", tenant_id)
    logger.info("用户 %s 注册成功 (tenant=%s)", req.username, tenant_name)
    return AuthResponse(
        token=token, user_id=user_id, username=req.username,
        display_name=req.display_name or req.username,
        role="viewer", tenant_id=tenant_id, tenant_name=tenant_name,
    )


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    async with get_pg_connection() as conn:
        user_row = conn.execute(
            """SELECT u.id, u.username, u.password_hash, u.display_name,
                      u.role, u.tenant_id, u.is_active, t.name, t.is_active AS tenant_active
               FROM t_user u
               LEFT JOIN t_tenant t ON u.tenant_id = t.id
               WHERE u.username = %s""",
            [req.username],
        ).fetchone()

    if not user_row:
        raise HTTPException(401, "用户名或密码错误")

    _, username, pw_hash, display_name, role, tenant_id, is_active, tenant_name, tenant_active = user_row

    if not is_active:
        raise HTTPException(401, "账号已被禁用，请联系管理员")

    if not bcrypt.checkpw(req.password.encode(), pw_hash.encode()):
        raise HTTPException(401, "用户名或密码错误")

    if tenant_id is not None and not tenant_active:
        raise HTTPException(401, "所属租户已被禁用，请联系管理员")

    token = create_jwt(user_row[0], username, role, tenant_id)
    logger.info("用户 %s 登录成功 (role=%s, tenant=%s)", username, role, tenant_name or "无")
    return AuthResponse(
        token=token, user_id=user_row[0], username=username,
        display_name=display_name or username,
        role=role or "viewer", tenant_id=tenant_id, tenant_name=tenant_name or "",
    )


def _verify_user(user_id: int) -> dict | None:
    """查询数据库确认用户/租户状态，结果缓存 30 秒。"""
    from src.storage.cache import get_memory_cache
    cache = get_memory_cache()
    cache_key = f"user_verify:{user_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    conn = _get_pg()
    try:
        row = conn.execute(
            """SELECT u.username, r.name AS role, u.tenant_id, t.is_active AS tenant_active
               FROM t_user u
               JOIN t_role r ON u.role_id = r.id
               LEFT JOIN t_tenant t ON u.tenant_id = t.id
               WHERE u.id = %s AND u.is_active = TRUE""",
            [user_id],
        ).fetchone()
        if not row:
            return None
        if row[3] is not None and not row[3]:
            return None
        result = {"username": row[0], "role": row[1], "tenant_id": row[2]}
        cache.set(cache_key, result, ttl=30)
        return result
    except Exception as exc:
        logger.warning("用户状态校验失败: %s", exc)
        return None
    finally:
        conn.close()


def get_current_user(authorization: str | None = Header(None)) -> dict:
    """Dependency: extract user from JWT Bearer token, verify against DB."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未提供有效的认证令牌")
    try:
        payload = decode_jwt(authorization[7:])
        user_id = payload["user_id"]

        info = _verify_user(user_id)
        if info is None:
            raise HTTPException(401, "用户不存在或已被禁用")

        return {
            "user_id": user_id,
            "username": info["username"],
            "role": info["role"],
            "tenant_id": info["tenant_id"],
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "认证已过期，请重新登录")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "认证无效")
