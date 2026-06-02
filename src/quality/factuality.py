"""事实一致性检查器 — 检测 LLM 回答是否基于检索上下文。

功能：
- 检查回答中的 claims 是否有上下文支撑
- 检查引用的来源是否对应实际内容
- 是否有编造数据/引用

特殊处理：
- "我不知道"/"没有找到" 回答 → 自动 pass，不浪费 LLM 调用
- 空 context → passed=True, score=0（无法验证但不是 LLM 的错）
- 调用失败 → 按 quality_fail_open_for_others 放行 + 记录警告

设计原因：
- 继承 QualityJudge 基类，复用 Prompt 加载、LLM 调用、JSON 解析
- 交叉评判：使用 quality_judge_model 作为评估模型，与生成模型不同
- 不调用 keyword_filter——那是 Safety 的职责
"""

from __future__ import annotations

import logging
from typing import Any

from src.quality.base import QualityJudge, QualityVerdict

logger = logging.getLogger(__name__)

# "我不知道"类回答的关键词匹配列表
# 覆盖常见的"不知道"、"没找到"、"无法回答"等表述变体
# 设计原则：宁可误匹配也不漏匹配——误匹配只是浪费一次 LLM 调用
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


class FactualityChecker(QualityJudge):
    """事实一致性检查器。

    判断 LLM 回答中的事实主张是否与检索到的参考资料一致。
    继承 QualityJudge 基类，复用 _load_prompt、_call_llm、_parse_json_response 等方法。

    Attributes:
        llm_provider: 实现了 BaseLLMProvider.chat() 接口的对象（与生成模型不同）
        config: 配置字典，支持 quality_judge_model 等键
    """

    def __init__(self, llm_provider, config: dict | None = None):
        """初始化 FactualityChecker。

        Args:
            llm_provider: 实现了 chat() 接口的 LLM 对象（如 MockLLMJudge 或真实 Provider）
            config: 配置字典，可包含 quality_judge_model、quality_judge_timeout_s 等
        """
        super().__init__(
            llm_provider=llm_provider,
            prompt_template_name="factuality_judge",
            config=config or {},
        )

    def evaluate(
        self,
        query: str,
        answer: str,
        context: str | list[str] | None = None,
    ) -> QualityVerdict:
        """评估模型回答的事实一致性。

        执行流程：
        1. 检测"不知道"类回答 → 自动通过，不调用 LLM Judge
        2. 检测上下文是否为空 → 通过但 score=0（无法验证）
        3. 调用 Judge LLM 进行交叉评判（使用旧接口兼容方法）
        4. 解析结果并返回 QualityVerdict
        5. 异常时按 fail-open 策略放行

        Args:
            query: 用户问题原文
            answer: 模型生成的回答文本
            context: 检索到的上下文。支持多种格式：
                - list[str]: 文档片段列表（旧接口格式）
                - str: 拼接后的上下文字符串
                - None / 空: 无检索结果

        Returns:
            QualityVerdict: 评估结果
                - passed: True 表示事实一致，False 表示存在幻觉
                - score: 0.0（完全幻觉）到 1.0（完全准确）
                - reasoning: 评估理由
                - details: 详细说明（与 reasoning 相同）
                - metadata: 包含 hallucinations 详情（如有）

        Raises:
            本方法不抛出异常 —— 异常时按 fail-open 返回 QualityVerdict
        """
        # 统一 context 为 list[str] 格式
        context_list = self._normalize_context(context)

        # ── Step 1: 检测"不知道"类回答 ──
        # 如果模型诚实地表示不知道，不判为幻觉
        if self._is_idk_answer(answer):
            logger.info("事实性评估跳过：回答为'不知道'类表述")
            return QualityVerdict(
                passed=True,
                score=1.0,
                reasoning="模型诚实地表示不知道，不判为幻觉",
                metadata={"note": "IDK answer detected, auto-passed"},
            )

        # ── Step 2: 空上下文处理 ──
        # 没有检索上下文时无法验证事实性，但不是模型编造的错
        if not context_list or all(not c.strip() for c in context_list):
            logger.info("事实性评估跳过：缺少检索上下文")
            return QualityVerdict(
                passed=True,
                score=0.0,
                reasoning="无检索上下文可验证",
                metadata={"note": "empty context, cannot verify but not hallucination"},
            )

        # ── Step 3: 调用 Judge LLM 进行评估 ──
        # 使用旧接口兼容方法（_load_prompt / _render_messages / _call_llm / _parse_json_response）
        # 而非新接口 _call_judge / _render_prompt，因为新接口在异常时返回 passed=False
        # （适用于 safety fail-closed），而事实性需要 fail-open（异常时 passed=True）
        try:
            # 3a. 加载 Prompt 模板
            template = self._load_prompt("factuality_judge.yaml")

            # 3b. 将上下文拼接为字符串（用分隔线隔开各文档片段）
            context_str = "\n---\n".join(context_list)

            # 3c. 渲染模板（插入 question / context / answer）
            messages = self._render_messages(
                template,
                question=query,
                context=context_str,
                answer=answer,
            )

            # 3d. 调用 LLM Judge
            response_text = self._call_llm(messages)

            # 3e. 解析 JSON 响应
            result = self._parse_json_response(response_text)

            # 3f. 构造 QualityVerdict 返回
            return self._build_verdict(result)

        except Exception as e:
            # ── Step 4: fail-open 策略 — 异常时放行 ──
            logger.warning(
                "事实性评估异常，按 fail-open 放行 (error=%s)",
                e,
                exc_info=True,
            )
            return QualityVerdict(
                passed=True,
                score=0.0,
                reasoning=f"事实性评估异常，按 fail-open 策略放行: {e}",
                metadata={"error": str(e)},
            )

    # ── 工具方法 ──────────────────────────────────────

    @staticmethod
    def _normalize_context(context: str | list[str] | None) -> list[str]:
        """将不同格式的 context 统一为 list[str]。

        Args:
            context: 支持 str、list[str]、None 三种格式

        Returns:
            list[str]: 标准化的上下文列表
        """
        if context is None:
            return []
        if isinstance(context, str):
            return [context] if context.strip() else []
        return context  # 已经是 list[str]

    def _is_idk_answer(self, answer: str) -> bool:
        """检测回答是否为"不知道"类表述。

        通过关键词子串匹配判断。设计为宽松匹配：
        - "没有找到相关信息" → 包含 "没有找到" → 匹配
        - "抱歉，我无法回答这个问题" → 包含 "我无法回答" → 匹配
        - "相关数据不在知识库中" → 包含 "不在知识库中" → 匹配

        Args:
            answer: 模型回答文本

        Returns:
            bool: True 表示是"不知道"类回答，应自动通过
        """
        if not answer:
            return False

        answer_lower = answer.lower()
        for pattern in _IDK_PATTERNS:
            if pattern.lower() in answer_lower:
                return True
        return False
