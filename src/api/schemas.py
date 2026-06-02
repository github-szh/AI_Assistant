"""Pydantic request/response models for all API endpoints."""

from datetime import datetime
from pydantic import BaseModel, Field

from src.config import settings


# ── Chat ───────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: list[dict[str, str]]
    provider: str = settings.llm_provider
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    session_id: str | None = None


class ChatResponse(BaseModel):
    content: str
    provider: str
    model: str


# ── Upload ─────────────────────────────────────────

class UploadResponse(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    status: str
    parser_used: str
    chunks_count: int | None = None
    message: str = ""


# ── Documents ──────────────────────────────────────

class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    status: str
    parser_used: str
    chunks_count: int | None = None
    file_size: str = ""
    pages: int | None = None
    uploaded_at: str = ""
    summary: str = ""


class DocumentDetail(DocumentInfo):
    chunks: list[str] = []


class DocumentListResponse(BaseModel):
    documents: list[DocumentInfo]
    total: int


# ── Query (RAG) ────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    doc_ids: list[str] | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    messages: list[dict] | None = None  # 最近几轮对话，用于查询改写


class SourceInfo(BaseModel):
    doc_id: str
    filename: str
    chunk_index: int | None = None
    score: float | None = None
    snippet: str = ""


# ── Quality (RAG) ───────────────────────────────────

class QualityVerdict(BaseModel):
    """单个质检维度的判定结果"""
    dimension: str = Field(description="质检维度: safety/factuality/relevance/retrieval_quality")
    passed: bool = Field(description="是否通过")
    score: float = Field(ge=0.0, le=1.0, description="得分 0-1")
    details: str = Field(default="", description="详细说明/违规原因")


class InterventionInfo(BaseModel):
    """干预引擎的决策信息"""
    intervened: bool = Field(description="是否被干预")
    action: str = Field(pattern="^(none|block|rewrite|warn|degrade)$", description="执行的动作: none/block/rewrite/warn/degrade")
    reason: str = Field(default="", description="干预原因")
    violations: list[QualityVerdict] = Field(default_factory=list, description="所有维度的判定详情")


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceInfo] = []
    quality: InterventionInfo | None = None  # 质检结果（可选，向后兼容）


# ── Agent ─────────────────────────────────────────

class AgentRequest(BaseModel):
    task: str
    session_id: str


class AgentEvent(BaseModel):
    """SSE event for streaming agent responses."""
    type: str  # "thought", "action", "observation", "answer", "error"
    content: str
    tool_name: str | None = None


# ── Sessions ──────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: str = ""


class SessionInfo(BaseModel):
    id: str
    title: str
    message_count: int
    created_at: str = ""


class SessionDetail(SessionInfo):
    messages: list[dict[str, str]] = []
    has_more: bool = False
    summary: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    total: int


# ── Delete Document ───────────────────────────────

class DeleteDocumentResponse(BaseModel):
    doc_id: str
    deleted: bool
    message: str = ""


# ── Health ─────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, str]
