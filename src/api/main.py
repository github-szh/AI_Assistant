"""FastAPI application entry point."""

import logging
import threading
import time
import traceback

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from src.api.middleware import LoggingMiddleware
from src.api.routes import health, chat, chat_stream, upload, documents, delete_document, query, sessions, auth, admin
from src.monitoring.metrics import collect_db_pool, collect_gpu, collect_redis, collect_resources, generate_latest
from src.monitoring.alerts import evaluate_alerts
from src.monitoring.dashboard import router as dashboard_router
from src.observability.logging_config import setup_logging
from src.observability.tracing import setup_tracing

logger = logging.getLogger(__name__)


def _resource_loop(interval: int = 15):
    # Run once immediately, then every interval
    while True:
        try:
            collect_resources()
            collect_db_pool()
            collect_gpu()
            collect_redis()
        except Exception:
            pass
        # Check alert rules after resource collection
        try:
            fired = evaluate_alerts()
            if fired:
                logger.warning("Alerts fired: %s", [f["label"] for f in fired])
        except Exception:
            pass
        time.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    from src.config import settings
    if settings.phoenix_enabled:
        setup_tracing()
    # Start background resource collector
    t = threading.Thread(target=_resource_loop, args=(15,), daemon=True)
    t.start()
    # Initialize monitoring DB
    try:
        from src.monitoring.storage import init_db, init_alerts
        init_db()
        init_alerts()
    except Exception:
        pass
    # Pre-load embedding model
    try:
        from src.knowledge.embeddings import get_embedding_manager
        get_embedding_manager()._ensure_model()
    except Exception:
        pass
    # Pre-load reranker model in background (~23s, don't block startup)
    def _warmup_reranker():
        try:
            from src.knowledge.reranker import get_reranker
            get_reranker().warmup()
        except Exception:
            pass
    threading.Thread(target=_warmup_reranker, daemon=True).start()
    yield
    # 关闭 reranker 子进程（Windows 上必须主动关闭，否则 reload 时子进程残留）
    try:
        from src.knowledge.reranker import get_reranker
        get_reranker().close()
    except Exception:
        pass
    # 关闭连接池，释放所有数据库连接
    from src.api.deps import close_pool
    await close_pool()
    # 关闭日志文件句柄（Windows 上必须主动释放，否则 reload 时新进程无法打开）
    from src.observability.logging_config import shutdown_logging
    shutdown_logging()


app = FastAPI(
    title="AI Assistant",
    version="0.1.0",
    description="Document Parsing + RAG + Agent — unified API",
    lifespan=lifespan,
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware)


# ── Global exception handler — logs all unhandled errors to file ──

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error(
        "Unhandled error: %s %s\n%s",
        request.method, request.url.path, tb,
    )
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# Prometheus metrics endpoint
@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain")

# Routes
app.include_router(dashboard_router)
app.include_router(health.router)
app.include_router(chat.router)
app.include_router(chat_stream.router)
app.include_router(upload.router)
app.include_router(documents.router)
app.include_router(delete_document.router)
app.include_router(query.router)
app.include_router(sessions.router)
app.include_router(auth.router)
app.include_router(admin.router)
