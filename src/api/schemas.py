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
    ground_truth: str | None = None  # 标准答案，用于答案正确性校验


class SourceInfo(BaseModel):
    doc_id: str
    filename: str
    chunk_index: int | None = None
    score: float | None = None
    snippet: str = ""


# ── Quality (RAG) ───────────────────────────────────
#
# ═══════════════════════════════════════════════════════════════
# POST /query/stream — SSE 事件类型文档
# ═══════════════════════════════════════════════════════════════
#
# 所有 SSE 事件均以 data: {json}\n\n 格式发送，前端 ReadableStream 逐行解析。
#
# 事件发送顺序：
#   steps → status → sources+confidence → (quality) → c × N → (quality) → done
#                                    ↑                    ↑
#                             检索完成后            LLM 生成完毕后、done 之前
#
# ── 现有事件类型 ────────────────────────────────────
#
# 1. steps（检索步骤）
#    字段: {"steps": [...]}
#    说明: 检索过程的步骤数组（向量检索、重排序等）
#    出现时机: 检索完成立即发送
#
# 2. status（检索状态）
#    字段: {"status": "found"} 或 {"status": "not_found"}
#    说明: "found" 表示找到相关内容
#    出现时机: sources 之前，steps 之后（如有）
#
# 3. sources + confidence（检索来源）
#    字段: {"sources": [...], "confidence": "high|medium|low"}
#    说明: 检索到的来源列表及整体置信度
#    出现时机: status 之后，LLM 生成之前
#
# 4. c（LLM 生成文本块）
#    字段: {"c": "一段文本"}
#    说明: LLM 逐字生成的文本块，前端追加到回答中
#    出现时机: LLM 生成过程中，多次发送
#
# 5. quality（质检结果）【新增】
#    字段: 见下方详细说明
#    说明: 内容质量检测结果，可触发干预动作
#    出现时机: LLM 生成完毕后、done 之前（仅当启用质检时发送）
#
# 6. done（流结束标记）
#    字段: {"done": True}
#    说明: 流式响应结束
#    出现时机: LLM 生成完毕（所有事件发送完毕后）
#
# 7. step（状态消息，异常路径）
#    字段: {"step": "not_found", "msg": "..."}
#    说明: 检索未找到内容时的消息
#    出现时机: 检索结果为空时（替代正常流程）
#
# 8. error（错误消息）
#    字段: {"error": "错误描述"}
#    说明: 服务端异常信息
#    出现时机: 向量库不可用等严重错误时
#
# ── quality 事件格式详解 ─────────────────────────────
#
# 根据 action 字段的不同取值，quality 事件有四种形态：
#
# 【通过 — action="none"】
#   {"type": "quality", "intervened": false, "action": "none",
#    "violations": [...]}
#   说明: 质检通过，无需干预。前端正常展示回答。
#   前端处理: 不做特殊处理，继续展示 LLM 回答
#
# 【拦截 — action="block"】
#   {"type": "quality", "intervened": true, "action": "block",
#    "reason": "safety_harmful_content: 检测到有害内容",
#    "override_answer": "抱歉，根据内容安全策略，无法展示此回答。",
#    "violations": [...]}
#   说明: 内容严重违规（如涉黄、涉政等），需完全屏蔽回答。
#   前端处理: 用 override_answer 替换已有的 LLM 回答内容
#
# 【警告 — action="warn"】
#   {"type": "quality", "intervened": true, "action": "warn",
#    "reason": "factuality_hallucination: 回答部分内容与原文不一致",
#    "warning_text": "此回答部分内容可能存在问题，请谨慎参考。",
#    "violations": [...]}
#   说明: 部分维度未通过（如事实性存疑），但回答仍可展示。
#   前端处理: 保留回答，在界面上追加警告提示
#
# 【降级 — action="degrade"】
#   {"type": "quality", "intervened": true, "action": "degrade",
#    "reason": "检索质量不足",
#    "degrade_reason": "检索结果质量较低，已自动降级。",
#    "violations": [...]}
#   说明: 检索质量不足以支撑完整回答，自动降级处理。
#   前端处理: 清空 LLM 回答内容，保留检索来源展示
#
# 注: quality 事件直接用 dict + json.dumps 发送，与现有 SSE 模式一致。
#     不需要为 quality 事件创建独立的 Pydantic 模型。
# ═══════════════════════════════════════════════════════════════

class VerdictDetail(BaseModel):
    """单个质检维度的判定结果（前端展示用）"""
    dimension: str = Field(description="质检维度")
    passed: bool = Field(description="是否通过")
    score: float = Field(ge=0.0, le=1.0, description="得分 0-1")
    details: str = Field(default="", description="详细说明")


class EvalResponse(BaseModel):
    """RAG 测评响应：回答 + 来源 + 四维度质检结果"""
    answer: str = Field(description="模型回答")
    sources: list["SourceInfo"] = Field(default_factory=list, description="检索来源")
    quality: dict[str, "VerdictDetail"] = Field(default_factory=dict, description="各维度质检结果")
    intervention: "InterventionInfo | None" = Field(default=None, description="干预决策")


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
