"""回答相关性审查器（RelevanceChecker）。

评估模型回答是否直接回应了用户问题，检查是否有离题或遗漏内容。
RelevanceChecker 是三个评判中最简单的：只比较 query 和 answer，不需要 context。

设计原则：
1. 不依赖 context — 仅判断"回答 vs 问题"本身
2. "我不知道"/"无法回答" — 视为 relevant（LLM 正确回应了问题，表示无法回答）
3. Fail-open — 调用失败时放行 + 记录警告，不阻断流程
4. 使用低温度（temperature=0.0）保证评估一致性
5. sources 过滤 — 接收来源文档信息用于日志追踪（评估本身不依赖 context）

使用示例：
    checker = RelevanceChecker(llm_provider=my_llm)
    verdict = checker.evaluate(
        query="什么是 RAG？",
        answer="RAG 是一种检索增强生成技术。",
    )
    print(verdict.passed, verdict.score, verdict.reasoning)
"""

from __future__ import annotations

import logging

from src.quality.base import QualityJudge, QualityVerdict

logger = logging.getLogger(__name__)

# 相关性评估使用的 Prompt 模板文件名
RELEVANCE_JUDGE_PROMPT = "relevance_judge.yaml"

# 评估任务标记，用于 MockLLMJudge 识别任务类型
EVALUATION_TASK_MARKER = "[EVALUATION_TASK] relevance"


class RelevanceChecker(QualityJudge):
    """回答相关性审查器。

    检查模型回答是否直接回应了用户问题，是否有离题或遗漏内容。
    RelevanceChecker 是三个评判中最简单的：只比较 query 和 answer，不需要 context。

    Attributes:
        threshold: 相关性得分阈值（0~1），低于此值记录警告但仍放行
            （RelevanceChecker 仅告警，不阻断）
    """

    def __init__(
        self,
        llm_provider,
        prompt_dir: str = "prompts/quality",
        judge_model: str | None = None,
        threshold: float = 0.7,
    ):
        """初始化 RelevanceChecker。

        Args:
            llm_provider: 实现了 BaseLLMProvider.chat() 接口的对象
            prompt_dir: Prompt YAML 模板目录路径
            judge_model: 评估使用的模型名，None 则用 llm_provider 默认
            threshold: 相关性阈值（0~1），低于此值日志告警。默认 0.7
        """
        super().__init__(llm_provider, prompt_dir, judge_model)
        self._threshold = threshold
        # 预加载 Prompt 模板（evaluate 时直接渲染，避免重复 IO）
        self._prompt_template = self._load_prompt(RELEVANCE_JUDGE_PROMPT)

    def evaluate(
        self,
        query: str,
        answer: str,
        context: str | None = None,  # 兼容接口，但不使用
        **kwargs,
    ) -> QualityVerdict:
        """执行相关性评估。

        RelevanceChecker 不需要 context 参数（仅看 answer 和 query），
        但为了兼容 QualityGuard 的统一调用接口，保留 context 参数但不使用。

        评估维度（由 LLM Judge 依据 Prompt 判断）：
        1. 是否直接回应了用户问题
        2. 是否有大段离题或无关内容
        3. 是否有遗漏的问题部分

        Args:
            query: 用户问题
            answer: 模型回答
            context: 兼容参数，不实际使用（仅用于保持接口一致）
            **kwargs: 其他兼容参数

        Returns:
            QualityVerdict:
                - 正常: 包含 LLM Judge 的评估结果
                - 异常: fail-open 返回 passed=True（默认放行）
        """
        try:
            # Step 1: 渲染 Prompt 模板为消息列表
            messages = self._render_messages(
                self._prompt_template,
                question=query,
                answer=answer,
            )

            # 注入评估任务标记（便于 MockLLMJudge 识别任务类型，
            # 也对真实 LLM 有提示作用）
            user_content = messages[1]["content"]
            messages[1]["content"] = f"{EVALUATION_TASK_MARKER}\n\n{user_content}"

            # Step 2: 调用 LLM Judge 获取评估
            response_text = self._call_llm(messages)

            # Step 3: 解析 LLM 返回的 JSON
            response = self._parse_json_response(response_text)

            # Step 4: 构建结构化评估结果
            verdict = self._build_verdict(response, dimension="relevance")

            # Step 5: 分数低于阈值时记录警告（但不禁用）
            if verdict.score < self._threshold:
                logger.warning(
                    "相关性评估：分数偏低 (score=%.2f < threshold=%.2f)，"
                    "query='%s'",
                    verdict.score,
                    self._threshold,
                    query[:50],  # 截断长 query，避免日志爆炸
                )

            return verdict

        except Exception as e:
            # Fail-open 策略：任何异常都放行并记录警告
            # 原因：相关性评估是"质量提升"而非"安全必须"，不应阻断正常回答
            logger.warning(
                "相关性评估调用失败，执行 fail-open 放行。错误: %s",
                str(e),
                exc_info=True,
            )
            return QualityVerdict(
                passed=True,
                dimension="relevance",
                score=0.7,  # 默认中等偏上分数（保守放行）
                reasoning=f"相关性评估调用失败，已自动放行。错误: {str(e)}",
            )
