"""FastAPI dependency injection — lightweight connection pool.

自建连接池：queue + semaphore，不依赖 psycopg_pool，行为完全可控。
"""

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache

import psycopg

from src.config import settings, Settings
from src.llm.router import LLMRouter, get_llm
from src.parsing.loader import DocumentLoader

logger = logging.getLogger(__name__)

# ── Custom connection pool ──────────────────────────────────


class _Pool:
    """极简连接池：信号量控制并发数，列表存储空闲连接。

    与 psycopg_pool 不同，这个池的行为完全透明，没有后台线程、
    没有隐式状态重置、没有 ProactorEventLoop 兼容问题。
    """

    def __init__(self, dsn: str, min_size: int = 5, max_size: int = 30, timeout: float = 30):
        self.dsn = dsn
        self.max_size = max_size
        self.timeout = timeout
        self._sem = threading.BoundedSemaphore(max_size)
        self._lock = threading.Lock()
        self._pool: list[psycopg.Connection] = []
        self._total_created = 0
        # 预热连接
        for _ in range(min_size):
            try:
                self._pool.append(psycopg.connect(dsn, connect_timeout=5))
                self._total_created += 1
            except Exception as exc:
                logger.warning("预热连接失败: %s", exc)
        logger.info("Pool ready: %d connections (max=%d)", len(self._pool), max_size)

    def getconn(self) -> psycopg.Connection:
        """获取连接，最多等待 timeout 秒。"""
        if not self._sem.acquire(timeout=self.timeout):
            raise PoolTimeout(
                f"couldn't get a connection after {self.timeout:.0f} sec "
                f"(pool: {len(self._pool)} free, {self._total_created} total)"
            )
        with self._lock:
            if self._pool:
                conn = self._pool.pop()
                # 验证连接可用性
                try:
                    conn.execute("SELECT 1")
                except Exception:
                    # 连接已死，创建新的
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._total_created -= 1
                    conn = self._connect_new()
                return conn
            return self._connect_new()

    def putconn(self, conn: psycopg.Connection) -> None:
        """归还连接，重置到 IDLE 状态。"""
        try:
            conn.rollback()
            conn.autocommit = True  # 切到 autocommit 让连接回到 IDLE
        except Exception:
            pass
        with self._lock:
            self._pool.append(conn)
        self._sem.release()

    def _connect_new(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, connect_timeout=5)
        self._total_created += 1
        return conn

    def close(self) -> None:
        with self._lock:
            for conn in self._pool:
                try:
                    conn.rollback()
                    conn.autocommit = True
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()

    @property
    def free(self) -> int:
        return len(self._pool)

    @property
    def total(self) -> int:
        return self._total_created


class PoolTimeout(Exception):
    """连接池耗尽异常。"""
    pass


_pool: _Pool | None = None


def _get_pool() -> _Pool:
    global _pool
    if _pool is None:
        dsn = (
            f"postgresql://{settings.pg_user}:{settings.pg_password}"
            f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
        )
        _pool = _Pool(dsn=dsn, min_size=5, max_size=30, timeout=30)
    return _pool


@asynccontextmanager
async def get_pg_connection():
    """异步上下文管理器 — 在线程池获取/归还连接，不阻塞事件循环。"""
    p = _get_pool()
    conn = await asyncio.to_thread(p.getconn)
    try:
        yield conn
    finally:
        await asyncio.to_thread(p.putconn, conn)


@contextmanager
def get_pg_connection_sync():
    """同步上下文管理器。"""
    p = _get_pool()
    conn = p.getconn()
    try:
        yield conn
    finally:
        p.putconn(conn)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await asyncio.wait_for(asyncio.to_thread(_pool.close), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Pool close timed out after 5s, forcing shutdown")
        except Exception:
            pass
        _pool = None
        logger.info("Pool closed")


def pool_stats() -> dict:
    if _pool is None:
        return {"free": 0, "total": 0}
    return {"free": _pool.free, "total": _pool.total}


# ── Other shared dependencies ────────────────────────────────


@lru_cache()
def get_settings() -> Settings:
    return settings


@lru_cache()
def get_document_loader() -> DocumentLoader:
    return DocumentLoader()
