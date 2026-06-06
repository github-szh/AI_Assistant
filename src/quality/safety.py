"""SafetyChecker — 安全维度质量检查器。

实现两阶段安全检查：
1. 关键词预过滤（快速路径）：命中关键词直接返回违规，不调用 LLM
2. LLM 语义评判（慢速路径）：未命中关键词时调用 LLM 深入判断

安全策略：
- 关键词命中 → 快速返回 QualityVerdict(passed=False, score=0.0)
- LLM 评判通过 → 返回 QualityVerdict(passed=True)
- 模型正确拒答（如"我无法回答"） → 自动判为通过（拒绝回答不安全问题属于正确行为）
- LLM 调用异常 → 根据 quality_fail_closed_for_safety 配置决定：
  - True（默认）: 阻断响应（安全优先）
  - False: 放行并记录告警
"""

from __future__ import annotations

import logging
from typing import Any

from src.api.schemas import QualityVerdict
from src.quality.base import QualityJudge
from src.quality.keyword_filter import KeywordFilter

logger = logging.getLogger(__name__)

# LLM 拒答模式列表（用于识别模型正确拒绝回答的场景）
# 当回答命中这些模式时，自动判为安全通过
_REFUSAL_PATTERNS = [
    # 中文拒答模式
    "无法回答",
    "无法提供",
    "我不能回答",
    "我不能提供",
    "我不能",
    "抱歉",
    "对不起",
    "我无法回答",
    "我无法提供",
    "我不认为",
    "没有相关信息",
    "拒绝回答",
    "不便回答",
    "无法满足",
    "没有找到相关信息",
    "无法处理",
    "无法回应",
    # 英文拒答模式
    "i cannot",
    "i can't",
    "i'm sorry",
    "i am sorry",
    "sorry",
    "cannot answer",
    "can't answer",
    "not able to",
    "cannot provide",
    "can't provide",
]


class SafetyChecker(QualityJudge):
    """安全维度质检器。

    先通过关键词预过滤快速检测，未命中时再调用 LLM 语义评判。
    使用 quality_fail_closed_for_safety 配置决定异常时的行为。

    Attributes:
        keyword_filter: KeywordFilter 实例，用于第一阶段快速预筛
    """

    def __init__(
        self,
        llm_provider: Any,
        config: dict | None = None,
        keyword_filter: KeywordFilter | None = None,
    ):
        """初始化 SafetyChecker。

        Args:
            llm_provider: 具有 chat() 方法的 LLM 实例
            config: 配置字典（支持 QualityJudge 的全部配置键）
            keyword_filter: 关键词过滤器实例，默认使用 KeywordFilter()
        """
        super().__init__(
            llm_provider=llm_provider,
            prompt_template_name="safety_judge",
            config=config,
        )
        self.keyword_filter = keyword_filter or KeywordFilter()

    def evaluate(
        self, query: str, answer: str, context: str | None = None, **kwargs
    ) -> QualityVerdict:
        """执行安全评估。

        两阶段流程：
        阶段1 — 关键词预过滤：
          在 query + answer 中搜索敏感关键词，命中即快速返回违规。
          不调用 LLM，极低延迟。

        阶段2 — LLM 语义评判：
          未命中关键词时，加载 safety_judge.yaml 模板并调用 LLM 做深度评估。
          处理模型拒答、LLM 异常等情况。

        Args:
            query: 用户问题
            answer: 模型生成的回答
            context: 检索上下文（可选，传入后可提高判断准确性）

        Returns:
            QualityVerdict:
                dimension="safety"
                passed: 是否通过安全检查
                score: 0.0（不安全）~ 1.0（完全安全）
                details: 详细说明/违规原因
        """
        # ════════════════════════════════════════════════
        # 阶段1：关键词预过滤（快速路径）
        # ════════════════════════════════════════════════
        keyword_matches = self.keyword_filter.prefilter(f"{query}\n{answer}")
        if keyword_matches:
            return self._build_keyword_verdict(keyword_matches)

        # ════════════════════════════════════════════════
        # 阶段2：LLM 语义评判（慢速路径）
        # ════════════════════════════════════════════════

        # 2a. 渲染 Prompt 模板
        try:
            prompt = self._render_prompt(
                self.prompt_template_name,
                question=query,
                answer=answer,
                context=context or "",
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            logger.error("Safety Prompt 渲染失败: %s", exc)
            return self._handle_judge_error(str(exc))

        # 2b. 调用 LLM Judge（含超时保护）
        result = self._call_judge(prompt)

        # 2c. 检查模型是否正确拒答
        # 如果模型拒绝了不安全问题，这属于正确行为，不应判为违规
        if self._is_refusal(answer):
            logger.info("Safety: 模型正确拒答，自动通过安全检查")
            return QualityVerdict(
                dimension="safety",
                passed=True,
                score=1.0,
                details="模型正确拒绝回答不安全问题，通过安全检查",
            )

        # 2d. 检查 LLM 调用是否出错（超时、解析失败等）
        if result.get("_error"):
            error_msg = result.get("reasoning", "未知错误")
            return self._handle_judge_error(error_msg)

        # 2e. 解析 LLM 评判结果
        passed = result.get("passed", True)
        score = result.get("score", 1.0)
        violations = result.get("violations", [])
        reasoning = result.get("reasoning", "")

        if not passed and violations:
            # 存在具体违规项，构造详细说明
            detail = "; ".join(
                f"[{v.get('type', 'unknown')}] {v.get('detail', '')}"
                f" (严重度: {v.get('severity', 'unknown')})"
                for v in violations[:5]  # 最多展示前5条
            )
            return QualityVerdict(
                dimension="safety",
                passed=False,
                score=score,
                details=detail or reasoning,
            )

        return QualityVerdict(
            dimension="safety",
            passed=passed,
            score=score,
            details=reasoning,
        )

    # ── 内部方法 ────────────────────────────────────────

    def _build_keyword_verdict(
        self, matches: list[Any]
    ) -> QualityVerdict:
        """根据关键词匹配结果构造违规判定。

        Args:
            matches: KeywordFilter.prefilter 返回的匹配列表

        Returns:
            QualityVerdict: 违规判定（passed=False）
        """
        detail = "; ".join(
            f"[{m.category}] 命中关键词「{m.keyword}」(位置 {m.position})"
            for m in matches[:10]  # 最多展示前10个
        )
        logger.info("Safety 关键词命中: %s", detail)
        return QualityVerdict(
            dimension="safety",
            passed=False,
            score=0.0,
            details=f"关键词命中: {detail}",
        )

    @staticmethod
    def _is_refusal(answer: str) -> bool:
        """判断模型回答是否属于正确的拒答行为。

        当模型拒绝回答不安全问题时，这属于安全行为，不应判为违规。

        Args:
            answer: 模型回答

        Returns:
            bool: True 表示为拒答，应自动通过安全检查
        """
        if not answer:
            return False
        answer_lower = answer.lower()
        for pattern in _REFUSAL_PATTERNS:
            if pattern in answer_lower:
                return True
        return False

    def _handle_judge_error(self, error_msg: str) -> QualityVerdict:
        """处理 Judge 异常情况。

        根据 quality_fail_closed_for_safety 配置决定行为：
        - True（默认）: 阻断响应（安全优先）
        - False: 放行并记录告警

        Args:
            error_msg: 错误描述

        Returns:
            QualityVerdict: 依据策略的判定结果
        """
        fail_closed = self.config.get("quality_fail_closed_for_safety", True)

        if fail_closed:
            logger.warning("Safety Judge 异常，依据 fail-closed 策略拦截: %s", error_msg)
            return QualityVerdict(
                dimension="safety",
                passed=False,
                score=0.0,
                details=f"安全审查异常（fail-closed）: {error_msg}",
            )
        else:
            logger.warning("Safety Judge 异常，依据 fail-open 策略放行: %s", error_msg)
            return QualityVerdict(
                dimension="safety",
                passed=True,
                score=1.0,
                details=f"安全审查异常（fail-open）: {error_msg}",
            )
