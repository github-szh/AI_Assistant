"""FastAPI application entry point."""

import logging
import traceback

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.middleware import LoggingMiddleware
from src.api.routes import health, chat, chat_stream, upload, documents, delete_document, query, sessions, auth, admin
from src.observability.logging_config import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Pre-load embedding model
    try:
        from src.knowledge.embeddings import get_embedding_manager
        get_embedding_manager()._ensure_model()
    except Exception:
        pass
    # Pre-load reranker model (avoids ~10s cold-start on first deep search)
    try:
        from src.knowledge.reranker import get_reranker
        get_reranker().warmup()
    except Exception:
        pass
    yield


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


# Routes
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
