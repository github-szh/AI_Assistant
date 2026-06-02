"""干预引擎 — 根据质检判定结果执行干预动作。

InterventionEngine 是 RAG 质量保证的决策执行层，负责：
1. 接收 Checker 层（SafetyChecker、FactualityChecker 等）产出的 QualityVerdict 列表
2. 根据配置的 InterventionRule 规则，按优先级决定是否干预、如何干预
3. 执行干预动作（BLOCK / DEGRADE / WARN / NONE），修改最终响应

使用流程：
    engine = InterventionEngine()
    verdicts = checker.evaluate_all(query, answer, context)
    intervention = engine.evaluate(verdicts)
    response = engine.execute(intervention, {"answer": "...", "sources": [...]})

或一步到位：
    response, intervention = engine.run_all(verdicts, {"answer": "...", "sources": [...]})
"""

from __future__ import annotations

import logging
from typing import Any

from src.api.schemas import InterventionInfo, QualityVerdict
from src.quality.config import InterventionRule, get_default_intervention_rules

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────

# 维度标识到违规类型前缀的映射
# 注意：dimension "retrieval_quality" 对应的 violation_type 前缀是 "retrieval"
# 例如：verdict.dimension == "retrieval_quality" 匹配 rule.violation_type == "retrieval_low_precision"
_DIMENSION_TO_PREFIX: dict[str, str] = {
    "safety": "safety",
    "factuality": "factuality",
    "retrieval_quality": "retrieval",
    "relevance": "relevance",
}

# 维度优先级映射（数字越小优先级越高）
# 用于对未通过的 verdict 排序，确保最高优先级维度最先匹配规则
_DIMENSION_PRIORITY: dict[str, int] = {
    "safety": 1,
    "factuality": 2,
    "retrieval_quality": 3,
    "relevance": 4,
}

# 阻断时的默认安全提示文案（不暴露任何原始内容）
_BLOCK_MESSAGE = "抱歉，根据内容安全策略，无法展示此回答。"

# 告警时追加到 answer 末尾的提示信息
_WARN_SUFFIX = "\n\n---\n⚠️ 此回答内容可能存在问题，请谨慎参考。"


class InterventionEngine:
    """干预引擎：根据质检判据执行规则驱动的干预动作。

    核心流程：
      1. evaluate(verdicts) → 按优先级匹配规则，产出 InterventionInfo
      2. execute(intervention, response) → 根据 action 修改响应
      3. run_all(verdicts, response) → 一次调用完成 evaluate + execute

    Attributes:
        rules: 按优先级排序的干预规则列表（priority=1 为最高优先级）
    """

    def __init__(self, rules: list[InterventionRule] | None = None) -> None:
        """初始化干预引擎，加载并按优先级排序规则。

        Args:
            rules: 干预规则列表。为 None 时使用 get_default_intervention_rules()。
                   规则将在初始化时按 priority 升序排列（1 = 最高优先级）。
        """
        self.rules = sorted(
            rules if rules is not None else get_default_intervention_rules(),
            key=lambda r: r.priority,
        )
        logger.debug("InterventionEngine 初始化，加载 %d 条规则", len(self.rules))

    # ── 评估 ─────────────────────────────────────────────

    def evaluate(self, verdicts: list[QualityVerdict]) -> InterventionInfo:
        """根据质检判据列表做出干预决策。

        算法步骤：
          1. 过滤出所有未通过检查的 verdict（passed=False）
          2. 按维度优先级排序（safety > factuality > retrieval_quality > relevance）
          3. 对每个未通过的 verdict，按规则优先级逐一匹配：
             - 使用 verdict.dimension 查找对应的 violation_type 前缀
             - 检查每条规则的 violation_type 是否以该前缀开头
             - 第一个匹配的规则胜出
          4. 无任何匹配时返回 intervened=False, action="none"

        Args:
            verdicts: 各质检维度的判据列表（来自 Checker 层）

        Returns:
            InterventionInfo: 干预决策信息，包含是否干预、动作、原因和所有判据
        """
        # 步骤1：过滤未通过的 verdict
        failed = [v for v in verdicts if not v.passed]

        if not failed:
            # 所有维度均通过 → 不干预
            return InterventionInfo(
                intervened=False,
                action="none",
                reason="",
                violations=verdicts,
            )

        # 步骤2：按维度优先级升序排列（1=最高，先处理）
        failed.sort(key=lambda v: _DIMENSION_PRIORITY.get(v.dimension, 99))

        # 步骤3 & 4：按规则优先级匹配第一个违规
        for verdict in failed:
            prefix = _DIMENSION_TO_PREFIX.get(verdict.dimension)
            if prefix is None:
                logger.warning("未知维度 '%s'，跳过匹配", verdict.dimension)
                continue

            for rule in self.rules:
                if rule.violation_type.startswith(prefix):
                    logger.info(
                        "规则命中：%s → action=%s（维度=%s）",
                        rule.violation_type,
                        rule.action,
                        verdict.dimension,
                    )
                    return InterventionInfo(
                        intervened=True,
                        action=rule.action,
                        reason=rule.message,
                        violations=verdicts,
                    )

        # 未找到匹配规则（理论上不会发生，因为默认规则覆盖了所有维度）
        logger.warning("未找到匹配规则的违规维度: %s", [v.dimension for v in failed])
        return InterventionInfo(
            intervened=False,
            action="none",
            reason="未找到匹配的干预规则",
            violations=verdicts,
        )

    # ── 执行 ─────────────────────────────────────────────

    @staticmethod
    def execute(
        intervention: InterventionInfo,
        original_response: dict[str, Any],
    ) -> dict[str, Any]:
        """根据干预决策执行动作，修改响应内容。

        各动作的行为：
          - BLOCK:  完全替换 answer 为安全提示，sources 置空，不暴露原内容
          - DEGRADE: answer 置空（""），sources 完整保留
          - WARN:    在原始 answer 末尾追加 ⚠️ 提示，sources 保持不变
          - NONE:    保持原始响应不变，附加 quality 信息

        Args:
            intervention: 干预决策（InterventionInfo）
            original_response: 原始响应字典，至少包含 "answer" 和 "sources" 键

        Returns:
            dict: 执行干预后的响应字典，包含 answer、sources 和 quality 信息
        """
        # 将 InterventionInfo 转为字典用于嵌入响应
        quality_dict = intervention.model_dump()
        action = intervention.action

        if action == "block":
            # 完全替换，不暴露原始内容
            return {
                "answer": _BLOCK_MESSAGE,
                "sources": [],
                "quality": quality_dict,
            }

        if action == "degrade":
            # answer 置空，sources 完整保留
            return {
                "answer": "",
                "sources": original_response.get("sources", []),
                "quality": quality_dict,
            }

        if action == "warn":
            # 在 answer 末尾追加警告标记，不修改 sources
            original_answer = original_response.get("answer", "")
            return {
                "answer": original_answer + _WARN_SUFFIX,
                "sources": original_response.get("sources", []),
                "quality": quality_dict,
            }

        # action == "none": 保持原始响应，附加 quality 信息
        return {
            "answer": original_response.get("answer", ""),
            "sources": original_response.get("sources", []),
            "quality": quality_dict,
        }

    # ── 一站式接口 ───────────────────────────────────────

    def run_all(
        self,
        verdicts: list[QualityVerdict],
        original_response: dict[str, Any],
    ) -> tuple[dict[str, Any], InterventionInfo]:
        """一站式执行评估 + 干预。

        相当于依次调用 evaluate() 和 execute()，适合最常用的场景。

        Args:
            verdicts: 各质检维度的判据列表
            original_response: 原始响应字典（至少包含 answer 和 sources）

        Returns:
            tuple[dict, InterventionInfo]:
                - 第一个元素：修改后的响应字典
                - 第二个元素：干预决策信息
        """
        intervention = self.evaluate(verdicts)
        modified_response = self.execute(intervention, original_response)
        return modified_response, intervention
