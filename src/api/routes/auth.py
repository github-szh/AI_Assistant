"""JWT-based authentication — login + register."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
import bcrypt
import jwt

from src.config import settings

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


def _get_pg():
    import psycopg
    return psycopg.connect(
        host=settings.pg_host, port=settings.pg_port,
        dbname=settings.pg_database, user=settings.pg_user,
        password=settings.pg_password, connect_timeout=5,
    )


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
async def register(req: RegisterRequest):
    if len(req.username) < 2 or len(req.password) < 4:
        raise HTTPException(400, "用户名至少2位，密码至少4位")

    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    conn = _get_pg()

    try:
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
                conn.close()
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
        row = conn.execute(
            """INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
               VALUES (%s, %s, %s, 'viewer', %s) RETURNING id""",
            [req.username, pw_hash, req.display_name or req.username, tenant_id],
        ).fetchone()
        conn.commit()
        user_id = row[0]
    except Exception as e:
        conn.close()
        if "unique" in str(e).lower():
            raise HTTPException(409, "用户名已存在")
        raise HTTPException(500, "注册失败")

    conn.close()
    token = create_jwt(user_id, req.username, "viewer", tenant_id)
    return AuthResponse(
        token=token, user_id=user_id, username=req.username,
        display_name=req.display_name or req.username,
        role="viewer", tenant_id=tenant_id, tenant_name=tenant_name,
    )


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    conn = _get_pg()
    # 权限与多租户：登录时同时查询 role 和 tenant_id
    row = conn.execute(
        """SELECT u.id, u.username, u.password_hash, u.display_name, u.role, u.tenant_id, t.name
           FROM t_user u
           LEFT JOIN t_tenant t ON u.tenant_id = t.id
           WHERE u.username = %s AND u.is_active = TRUE
             AND (u.tenant_id IS NULL OR t.is_active = TRUE)""",
        [req.username],
    ).fetchone()
    conn.close()

    if not row or not bcrypt.checkpw(req.password.encode(), row[2].encode()):
        raise HTTPException(401, "用户名或密码错误")

    token = create_jwt(row[0], row[1], row[4], row[5])
    return AuthResponse(
        token=token, user_id=row[0], username=row[1],
        display_name=row[3] or row[1],
        role=row[4] or "viewer", tenant_id=row[5], tenant_name=row[6] or "",
    )


def get_current_user(authorization: str | None = Header(None)) -> dict:
    """Dependency: extract user from JWT Bearer token."""
    # 权限与多租户：返回 role 和 tenant_id
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未提供有效的认证令牌")
    try:
        payload = decode_jwt(authorization[7:])
        return {
            "user_id": payload["user_id"],
            "username": payload["username"],
            "role": payload.get("role", "viewer"),
            "tenant_id": payload.get("tenant_id"),
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "认证已过期，请重新登录")
    except Exception:
        raise HTTPException(401, "认证无效")
