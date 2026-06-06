"""零Token事实一致性检查器 — 使用向量匹配替代 LLM Judge。

通过将回答中的主张（claims）向量化，在来源文档片段中进行向量检索，
判断每个主张是否有足够的语义支撑，从而零成本完成事实性评估。

设计决策：
- 不调用任何 LLM，完全基于向量检索（cosine similarity）
- 仅在指定来源文档（doc_ids）范围内搜索，避免跨文档误匹配
- 支持父块二次验证（parent chunk verification），提升召回
- "不知道"类回答自动通过，不浪费计算资源
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.api.schemas import QualityVerdict
from src.config import settings
from src.knowledge.embeddings import get_embedding_manager
from src.quality.base import QualityJudge

logger = logging.getLogger(__name__)

# ── "不知道"类回答关键词（与 factuality.py _IDK_PATTERNS 保持一致） ──
_IDK_PATTERNS = [
    "我不知道",
    "没有找到",
    "无法找到",
    "无法回答",
    "没有相关信息",
    "知识库中没有",
    "抱歉，我无法",
    "抱歉，我没有",
    "对不起，我没有",
    "对不起，我无法",
    "暂未找到",
    "未能找到",
    "没有相关的信息",
    "无法提供",
    "不能回答",
    "不在我的知识范围内",
    "不在知识库中",
    "我无法提供",
    "我无法回答",
    "我没有找到",
    "我找不到",
]

# ── 主张提取时过滤的模式（问候、总结、过渡句） ──
_SKIP_CLAIM_PATTERNS = [
    "你好",
    "您好",
    "感谢",
    "谢谢",
    "根据以上",
    "综上所述",
    "总的来说",
    "以下是",
]

# ── 相似度阈值 ──
_THRESHOLD_SUPPORTED = 0.75  # >= 0.75 → 有支撑
_THRESHOLD_PARTIAL = 0.50  # 0.5–0.75 → 部分支撑, < 0.5 → 幻觉

# ── 聚合判定阈值 ──
_HALLUCINATION_FAIL_RATE = 0.30  # > 30% 幻觉 → 不通过
_PARTIAL_FAIL_RATE = 0.50  # > 50% 部分支撑 → 不通过

# ── 父块二次验证的最小词汇重叠率 ──
_PARENT_OVERLAP_THRESHOLD = 0.30


class VectorFactualityChecker(QualityJudge):
    """零Token事实一致性检查器。

    通过将回答中的主张向量化后在来源文档中检索，判断每个主张是否有语义支撑。
    完全基于向量匹配，不调用任何 LLM，实现零 Token 成本的事实性评估。

    使用方式：:

        checker = VectorFactualityChecker()
        verdict = checker.evaluate(query, answer, sources=["doc_1", "doc_2"])

    也可通过 QualityGuard 编排（此时 sources 为 None 时自动跳过）::

        guard = QualityGuard(
            checkers={"factuality": VectorFactualityChecker()},
            intervention=intervention,
            config=settings,
        )

    Attributes:
        llm_provider: 为兼容基类接口保留（不使用）
        config: 配置字典
    """

    def __init__(
        self,
        llm_provider: Any = None,
        config: dict | None = None,
    ) -> None:
        """初始化 VectorFactualityChecker。

        Args:
            llm_provider: 为兼容 QualityJudge 基类接口保留（不使用）
            config: 配置字典
        """
        super().__init__(llm_provider=llm_provider, config=config or {})
        self._idk_patterns = _IDK_PATTERNS

    # ── 主入口 ──────────────────────────────────────────

    def evaluate(
        self,
        query: str,
        answer: str,
        context: str | None = None,
        sources: list[str] | None = None,
    ) -> QualityVerdict:
        """评估回答的事实一致性（零 Token 成本）。

        执行流程：
        1. 检测"不知道"类回答 → 自动通过
        2. 无可用来源 → 通过但 score=0
        3. 从回答中提取事实主张（claims）
        4. 向量化所有主张
        5. 在来源文档片段中检索每个主张
        6. 可选：父块二次验证（当子块匹配度不足时）
        7. 按阈值判定每个主张：支撑/部分支撑/幻觉
        8. 聚合所有主张得到最终 verdict

        Args:
            query: 用户原始问题（用于日志记录）
            answer: 模型生成的回答文本
            context: 检索上下文（为兼容基类接口保留，不使用）
            sources: 来源文档 ID 列表。支持多种格式：
                - ``list[str]``: doc_id 字符串列表
                - ``list[dict]``: 包含 ``"doc_id"`` 或 ``"source"`` 键的字典列表
                - ``list[SourceInfo]``: 包含 ``.doc_id`` 属性的对象列表
                - ``None``: 无可用来源

        Returns:
            QualityVerdict: 评估结果
                - passed: True 表示事实一致，False 表示存在幻觉
                - score: 0.0（完全幻觉）到 1.0（完全准确）
                - details: 各主张的验证详情
        """
        # ── Step 1: 检测"不知道"类回答 ──
        if self._is_idk(answer):
            logger.info("向量事实性评估跳过：回答为'不知道'类表述")
            return QualityVerdict(
                dimension="factuality",
                passed=True,
                score=1.0,
                details="模型诚实地表示不知道，不判为幻觉",
            )

        # ── Step 2: 无可用来源 ──
        doc_ids = self._extract_doc_ids(sources)
        if not doc_ids:
            logger.info("向量事实性评估跳过：无可用来源文档")
            return QualityVerdict(
                dimension="factuality",
                passed=True,
                score=0.0,
                details="无来源文档可验证",
            )

        # ── Step 3: 提取主张 ──
        claims = self._extract_claims(answer)
        if not claims:
            logger.info("向量事实性评估跳过：回答中无有效主张")
            return QualityVerdict(
                dimension="factuality",
                passed=True,
                score=1.0,
                details="回答中无事实主张需要验证",
            )

        logger.debug(
            "向量事实性评估: %d 个主张, %d 个来源文档",
            len(claims),
            len(doc_ids),
        )

        # ── Step 4: 向量化所有主张 ──
        emb_mgr = get_embedding_manager()
        claim_embeddings = emb_mgr.encode(claims)

        # ── Step 5 & 6: 逐个检索并判定 ──
        claim_results: list[dict[str, Any]] = []
        for claim, emb in zip(claims, claim_embeddings):
            result = self._search_source_chunks(emb, doc_ids)
            similarity = result["similarity"]

            # Step 5a: 阈值初次判定
            status = self._judge_claim_by_similarity(similarity)

            # Step 6: 父块二次验证（仅对部分支撑或幻觉的主张）
            if result.get("parent_id") and status in ("partial", "hallucination"):
                parent_supported = self._verify_with_parent(
                    result["parent_id"], claim
                )
                if parent_supported:
                    status = "supported"
                    similarity = max(similarity, _THRESHOLD_SUPPORTED)

            claim_results.append({
                "claim": claim,
                "similarity": similarity,
                "status": status,
            })

        # ── Step 7: 聚合判定 ──
        return self._aggregate_verdict(claim_results)

    # ── 主张提取 ──────────────────────────────────────────

    @staticmethod
    def _extract_claims(answer: str) -> list[str]:
        """从回答中提取事实主张。

        处理流程：
        1. 移除 ``[来源:N]`` 标记
        2. 按句号（。）、分号（；）和换行（\\n）分割
        3. 过滤空串、过短（< 4 字符）的主张
        4. 过滤问候/总结/过渡类模板语句

        Args:
            answer: 模型回答文本

        Returns:
            list[str]: 提取到的事实主张列表
        """
        # 1. 移除 [来源:N] 标记
        text = re.sub(r"\[来源:\d+\]", "", answer)

        # 2. 按句号、分号、换行分割
        raw_claims = re.split(r"[。；\n]", text)

        # 3 & 4. 过滤空串、过短、模板语句
        claims: list[str] = []
        for c in raw_claims:
            c = c.strip()
            if not c or len(c) < 4:
                continue
            if any(p in c for p in _SKIP_CLAIM_PATTERNS):
                continue
            claims.append(c)

        return claims

    # ── IDK 检测 ─────────────────────────────────────────

    def _is_idk(self, answer: str) -> bool:
        """检测回答是否为"不知道"类表述。

        Args:
            answer: 模型回答文本

        Returns:
            bool: True 表示是"不知道"类回答
        """
        if not answer:
            return False
        answer_lower = answer.lower()
        for pattern in self._idk_patterns:
            if pattern.lower() in answer_lower:
                return True
        return False

    # ── 来源文档 ID 提取 ─────────────────────────────────

    @staticmethod
    def _extract_doc_ids(sources: Any) -> list[str]:
        """从 sources 中提取文档 ID 列表。

        支持多种输入格式，便于与不同调用方兼容：
        - ``list[str]``: 直接作为 doc_id 列表
        - ``list[dict]``: 优先取 ``"doc_id"`` 键，回退到 ``"source"`` 键
        - ``list[SourceInfo]``: 从 ``.doc_id`` 属性取值
        - ``None`` / 空列表: 返回空列表

        Args:
            sources: 来源信息，支持多种格式

        Returns:
            list[str]: 去重后的文档 ID 列表
        """
        if not sources:
            return []

        doc_ids: list[str] = []
        for s in sources:
            if isinstance(s, str):
                doc_ids.append(s)
            elif isinstance(s, dict):
                did = s.get("doc_id") or s.get("source")
                if did:
                    doc_ids.append(str(did))
            elif hasattr(s, "doc_id"):
                did = s.doc_id
                if did:
                    doc_ids.append(str(did))
            elif hasattr(s, "source"):
                did = s.source
                if did:
                    doc_ids.append(str(did))

        # 去重
        return list(set(doc_ids))

    # ── 向量检索 ─────────────────────────────────────────

    @staticmethod
    def _search_source_chunks(
        claim_embedding: list[float],
        doc_ids: list[str],
    ) -> dict[str, Any]:
        """在指定来源文档中检索与主张最相似的片段。

        仅搜索 ``data_documents`` 表中 ``metadata_->>'doc_id'`` 属于
        ``doc_ids`` 列表的记录，使用 cosine 距离（``<=>``）衡量语义相似度。

        Args:
            claim_embedding: 主张的向量表示
            doc_ids: 允许搜索的文档 ID 列表

        Returns:
            dict: 包含以下键：
                - ``text``: 最相似片段的文本
                - ``parent_id``: 父块 ID（可能为 None）
                - ``similarity``: 余弦相似度（0~1）
        """
        import psycopg

        try:
            conn = psycopg.connect(settings.pg_dsn, connect_timeout=5)
            rows = conn.execute(
                """
                SELECT d.text,
                       d.metadata_->>'parent_id' AS parent_id,
                       1 - (d.embedding <=> %s::vector) AS similarity
                FROM data_documents d
                WHERE d.metadata_->>'doc_id' = ANY(%s)
                ORDER BY similarity DESC
                LIMIT 1
                """,
                [claim_embedding, doc_ids],
            ).fetchall()
            conn.close()

            if rows and rows[0][2] is not None:
                r = rows[0]
                return {
                    "text": r[0] or "",
                    "parent_id": r[1],
                    "similarity": float(r[2]),
                }
            return {"text": "", "parent_id": None, "similarity": 0.0}
        except Exception as exc:
            logger.warning("向量检索失败: %s", exc)
            return {"text": "", "parent_id": None, "similarity": 0.0}

    # ── 父块二次验证 ─────────────────────────────────────

    @staticmethod
    def _verify_with_parent(parent_id: str, claim: str) -> bool:
        """通过父块内容二次验证主张。

        当子块匹配度不足时，检查父块（更大上下文窗口）中是否包含
        主张的关键词汇。使用词汇重叠率判断语义一致性。

        Args:
            parent_id: 父块 ID
            claim: 主张文本

        Returns:
            bool: True 表示父块内容支持该主张
        """
        from src.knowledge.index_store import _fetch_parent_contexts

        try:
            parents = _fetch_parent_contexts([parent_id])
            if not parents or parent_id not in parents:
                return False

            parent_text = parents[parent_id].get("content", "")
            if not parent_text:
                return False

            # 计算主张词汇与父块内容的重叠率
            claim_terms = set(claim.split())
            parent_terms = set(parent_text.split())
            overlap = len(claim_terms & parent_terms) / max(
                len(claim_terms), 1
            )

            return overlap > _PARENT_OVERLAP_THRESHOLD
        except Exception as exc:
            logger.debug("父块二次验证异常: %s", exc)
            return False

    # ── 单条主张阈值判定 ─────────────────────────────────

    @staticmethod
    def _judge_claim_by_similarity(similarity: float) -> str:
        """根据相似度判定单条主张的支撑状态。

        Args:
            similarity: 余弦相似度（0~1）

        Returns:
            str: ``"supported"`` / ``"partial"`` / ``"hallucination"``
        """
        if similarity >= _THRESHOLD_SUPPORTED:
            return "supported"
        elif similarity >= _THRESHOLD_PARTIAL:
            return "partial"
        else:
            return "hallucination"

    # ── 聚合判定 ─────────────────────────────────────────

    @staticmethod
    def _aggregate_verdict(claim_results: list[dict]) -> QualityVerdict:
        """聚合所有主张的判定结果，生成最终 verdict。

        聚合逻辑：
        - 无主张 → 通过，score=1.0
        - `>30%` 幻觉 → 不通过，score=0.0
        - `>50%` 部分支撑 → 不通过，score=0.0
        - 否则 → 通过，score=所有主张的平均支持度

        Args:
            claim_results: 各主张的判定结果列表。
                每个元素包含 ``claim``、``similarity``、``status`` 键。

        Returns:
            QualityVerdict: 聚合后的事实性评估结果
        """
        total = len(claim_results)
        if total == 0:
            return QualityVerdict(
                dimension="factuality",
                passed=True,
                score=1.0,
                details="无事实主张需要验证",
            )

        hallucinated = sum(
            1 for r in claim_results if r["status"] == "hallucination"
        )
        partial = sum(
            1 for r in claim_results if r["status"] == "partial"
        )
        supported = total - hallucinated - partial

        # 构建详细说明
        detail_parts = [f"共 {total} 条主张: {supported} 条有支撑"]
        if partial:
            detail_parts.append(f"{partial} 条部分支撑")
        if hallucinated:
            detail_parts.append(f"{hallucinated} 条无支撑")
        details = "，".join(detail_parts)

        hallucination_rate = hallucinated / total
        partial_rate = partial / total

        # 判定是否通过
        if hallucination_rate > _HALLUCINATION_FAIL_RATE:
            return QualityVerdict(
                dimension="factuality",
                passed=False,
                score=0.0,
                details=(
                    f"{details}（幻觉率 {hallucination_rate:.0%} > "
                    f"{_HALLUCINATION_FAIL_RATE:.0%}）"
                ),
            )

        if partial_rate > _PARTIAL_FAIL_RATE:
            return QualityVerdict(
                dimension="factuality",
                passed=False,
                score=0.0,
                details=(
                    f"{details}（部分支撑率 {partial_rate:.0%} > "
                    f"{_PARTIAL_FAIL_RATE:.0%}）"
                ),
            )

        # 通过：平均支持度作为得分
        avg_support = sum(r["similarity"] for r in claim_results) / total
        return QualityVerdict(
            dimension="factuality",
            passed=True,
            score=round(avg_support, 4),
            details=f"{details}，平均支持度 {avg_support:.1%}",
        )
