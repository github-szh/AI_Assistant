"""FastAPI dependency injection — with psycopg connection pool.

连接池复用数据库连接，避免每个请求都创建新连接（TCP 握手），
高并发下性能提升 5~10 倍。
"""

import logging
from functools import lru_cache

from psycopg_pool import ConnectionPool  # psycopg 连接池，需 pip install psycopg-pool

from src.config import settings, Settings
from src.llm.router import LLMRouter, get_llm
from src.parsing.loader import DocumentLoader

logger = logging.getLogger(__name__)

# ── Connection pool (singleton) ──────────────────────────────

# 全局连接池单例，模块首次导入时为空，首次调用 get_pg_connection() 时初始化
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    """Get or create the shared connection pool.
    
    Uses psycopg_pool.ConnectionPool with lazy initialisation.
    Requires psycopg_pool >= 3.1 (pip install psycopg-pool).

    连接池复用数据库连接，避免每次请求都创建新连接，减少 TCP 握手开销。
    """
    global _pool
    if _pool is None:
        dsn = (
            f"postgresql://{settings.pg_user}:{settings.pg_password}"
            f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
        )
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=4,      # 最少保持 4 个空闲连接
            max_size=20,     # 最大 20 个并发连接（原10，因连接泄漏可能耗尽）
            timeout=10,      # 等待连接的超时秒数（原5）
            max_lifetime=300,  # 连接最多存活 5 分钟后回收，防止泄漏堆积
            open=False,      # 先创建对象，首次使用时再真正打开（惰性加载）
        )
        _pool.open()
        logger.info("Connection pool created (min=2, max=20)")
    return _pool


def get_pg_connection():
    """临时停用连接池（2026-06-06）：返回直连，排查池停止响应问题。
    
    原来的 ConnectionPool 因连接泄漏在负载下耗尽，pool.getconn() 同步阻塞
    asyncio 事件循环，导致整个服务"停止响应"。
    
    改为直连后每个请求创建新 TCP 连接，消除池耗尽问题。
    待泄漏源修复后再切回池模式。
    
    TODO: 修复连接泄漏后切回 pool.getconn()
    """
    import psycopg
    from src.config import settings
    logger.warning(
        "Pool disabled: creating direct connection to %s:%s",
        settings.pg_host, settings.pg_port,
    )
    return psycopg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        connect_timeout=5,
    )


def pool_stats() -> dict:
    """返回连接池当前状态（用于调试）"""
    pool = _get_pool()
    return {
        "min": pool.min_size,
        "max": pool.max_size,
        "free": len(pool._pool) if hasattr(pool, "_pool") else -1,
        "requests_waiting": pool._nrequests if hasattr(pool, "_nrequests") else -1,
    }


# ── Other shared dependencies ────────────────────────────────


@lru_cache()
def get_settings() -> Settings:
    return settings


@lru_cache()
def get_document_loader() -> DocumentLoader:
    return DocumentLoader()


# LLM router is already a singleton via get_llm()
