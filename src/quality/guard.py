"""QualityGuard 编排类 — 统筹所有质检维度的执行与干预。

QualityGuard 是 RAG 质量保证的编排层（Orchestrator），负责：
1. 协调多个质检维度的运行（检索质量、安全、事实性、相关性）
2. 支持并行执行非安全维度评估（降低总延迟）
3. 将各维度评估结果汇总交给 InterventionEngine
4. 执行干预动作并返回修改后的响应

使用流程：:

    guard = QualityGuard(checkers, intervention, settings)
    modified_response, intervention_info = guard.run(
        query=question, answer=answer, context=context, sources=sources,
    )

架构位置：:

    QueryEngine.query()
        │
        ├─ RetrievalQualityChecker.should_skip_llm()  ← 预生成检查（零延迟）
        │
        ├─ LLM Generation
        │
        └─ QualityGuard.run()  ← 本类
              │
              ├─ RetrievalQualityChecker.evaluate()  ← 纯数值
              ├─ SafetyChecker.evaluate()             ← LLM 评判
              ├─ FactualityChecker.evaluate()          ← LLM 评判
              ├─ RelevanceChecker.evaluate()           ← LLM 评判
              │
              └─ InterventionEngine.run_all()          ← 干预执行

设计决策：
- 并行执行：Safety/Factuality/Relevance 三个 LLM 评判使用 ThreadPoolExecutor 并发
- 超时保护：concurrent.futures.wait() 带总超时，单个超时不阻塞其他维度
- 异常保护：单个 checker 失败时记录日志，继续执行其他维度（fail-isolated）
- 非 LLM 维度（检索质量）先行执行：纯数值计算，零延迟，不参与并行
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

from src.api.schemas import InterventionInfo, QualityVerdict
from src.config import Settings
from src.quality.base import QualityJudge
from src.quality.intervention import InterventionEngine
from src.quality.retrieval_quality import RetrievalQualityChecker

logger = logging.getLogger(__name__)

# LLM 评判维度列表（需要调用 LLM 的维度）
_LLM_DIMENSIONS = ["safety", "factuality", "answer_correctness", "relevance"]

# 维度名称的可读中文映射（用于日志输出）
_DIMENSION_LABELS: dict[str, str] = {
    "safety": "安全",
    "factuality": "事实性",
    "answer_correctness": "答案正确性",
    "relevance": "相关性",
    "retrieval_quality": "检索质量",
}


class QualityGuard:
    """质量保证编排类 — 统筹所有质检维度的执行与干预。

    Attributes:
        checkers: 质检器字典，键为维度名（"safety"/"factuality"/"relevance"），
                  值为对应的 QualityJudge 子类实例。
        intervention: InterventionEngine 实例，用于执行干预动作。
        config: Settings 全局配置对象，读取 quality_* 配置项。
        _retrieval_checker: 检索质量检查器（纯数值计算，非 QualityJudge）。
    """

    def __init__(
        self,
        checkers: dict[str, QualityJudge],
        intervention: InterventionEngine,
        config: Settings,
    ) -> None:
        """初始化 QualityGuard。

        Args:
            checkers: 质检器字典。键为维度名（"safety"/"factuality"/"relevance"），
                      值为对应的 QualityJudge 子类实例。
            intervention: InterventionEngine 实例，负责执行干预动作。
            config: Settings 全局配置对象，用于读取 quality_* 配置项。
        """
        self.checkers = checkers
        self.intervention = intervention
        self.config = config
        # 检索质量检查器：纯数值计算，不涉及 LLM 调用
        self._retrieval_checker = RetrievalQualityChecker()

        logger.info(
            "QualityGuard 初始化完成，加载 %d 个质检器: %s",
            len(checkers),
            list(checkers.keys()),
        )

    # ── 主入口 ──────────────────────────────────────────

    def run(
        self,
        query: str,
        answer: str,
        context: str,
        sources: list,
        **kwargs,
    ) -> tuple[dict[str, Any], InterventionInfo]:
        """执行全维度质量评估与干预。

        执行流程：
        1. 检索质量评估（纯数值计算，零延迟）
        2. 并行或串行执行 Safety/Factuality/Relevance 评估
           （由 ``config.quality_parallel_eval`` 控制）
        3. 汇总所有 verdicts 送入 InterventionEngine
        4. 执行干预动作（BLOCK / DEGRADE / WARN / NONE）
        5. 返回 (modified_response, intervention_info)

        Args:
            query: 用户原始问题。
            answer: LLM 生成的回答文本。
            context: 检索到的上下文字符串（用于事实性和相关性评估）。
            sources: 来源列表。元素可以是 SourceInfo（Pydantic 模型）或 dict，
                     需包含 ``score`` 字段用于检索质量评估。

        Returns:
            tuple[dict, InterventionInfo]:
                - 第一个元素：干预后的响应字典，包含 ``answer``、``sources``、``quality`` 三个键。
                - 第二个元素：干预决策详情（InterventionInfo Pydantic 模型）。
        """
        verdicts: list[QualityVerdict] = []

        # ── 步骤 1：检索质量评估（纯数值，零延迟） ──────
        self._run_retrieval_quality(query, sources, verdicts)

        # ── 步骤 2：LLM 评判维度（安全 / 事实性 / 相关性） ──
        available = {
            name: checker
            for name, checker in self.checkers.items()
            if name in _LLM_DIMENSIONS
        }

        if not available:
            logger.debug("无可用 LLM 质检器，跳过安全/事实性/相关性评估")
        elif self.config.quality_parallel_eval:
            self._run_parallel(available, query, answer, context, verdicts, sources=sources, **kwargs)
        else:
            self._run_sequential(available, query, answer, context, verdicts, sources=sources, **kwargs)

        # ── 步骤 3 & 4：汇总 → 干预引擎 → 执行干预 ──────
        original_response: dict[str, Any] = {
            "answer": answer,
            "sources": sources,
        }
        modified_response, intervention = self.intervention.run_all(
            verdicts, original_response,
        )

        logger.info(
            "QualityGuard 完成: %d 个维度评估, 干预=%s, action=%s",
            len(verdicts),
            intervention.intervened,
            intervention.action,
        )

        return modified_response, intervention

    # ── 步骤拆分方法 ─────────────────────────────────────

    def _run_retrieval_quality(
        self,
        query: str,
        sources: list,
        verdicts: list[QualityVerdict],
    ) -> None:
        """执行检索质量评估（纯数值计算），结果追加到 verdicts。

        从 sources 中提取 score 字段，计算平均分、最高分、离散度等指标。
        无分数数据时（如 sources 为空或缺少 score）跳过此步骤。

        Args:
            query: 用户问题（透传给 RetrievalQualityChecker.evaluate）。
            sources: 来源列表。
            verdicts: 用于收集评估结果的列表（原地追加）。
        """
        try:
            scores = self._extract_scores(sources)
            if not scores:
                logger.debug("检索质量评估跳过：sources 中无分数数据")
                return

            verdict = self._retrieval_checker.evaluate(query, scores)
            verdicts.append(verdict)
            logger.debug(
                "检索质量评估完成: passed=%s, score=%.4f",
                verdict.passed,
                verdict.score,
            )
        except Exception as exc:
            logger.warning("检索质量评估异常，已跳过: %s", exc)

    def _run_parallel(
        self,
        checkers: dict[str, QualityJudge],
        query: str,
        answer: str,
        context: str,
        verdicts: list[QualityVerdict],
        sources: list | None = None,
        **kwargs,
    ) -> None:
        """并行执行多个 LLM 质检维度的 evaluate() 方法。

        使用 ``concurrent.futures.ThreadPoolExecutor`` 并发运行。
        总超时 = ``quality_judge_timeout_s × checker数 + 5秒`` 缓冲。
        单个 checker 超时或异常不影响其他维度（fail-isolated）。

        Args:
            checkers: 需要并行执行的质检器字典。
            query: 用户问题。
            answer: 模型回答。
            context: 检索上下文。
            verdicts: 用于收集评估结果的列表（原地追加）。
            sources: 来源列表（透传至支持 sources 的 checker，如 VectorFactualityChecker）。
        """
        n = len(checkers)
        timeout_per = self.config.quality_judge_timeout_s
        total_timeout = timeout_per * n + 5  # 每个 checker 独立超时，加缓冲

        with ThreadPoolExecutor(max_workers=n) as executor:
            future_to_name = {
                executor.submit(checker.evaluate, query, answer, context, sources=sources, **kwargs): name
                for name, checker in checkers.items()
            }

            done: set = set()
            not_done: set = set()
            try:
                done, not_done = wait(
                    future_to_name,
                    timeout=total_timeout,
                    return_when="ALL_COMPLETED",
                )
            except Exception as exc:
                logger.error("parallel wait 异常: %s", exc)
                done = set(future_to_name.keys())
                not_done = set()

            # 处理完成的 futures
            for future in done:
                name = future_to_name[future]
                label = _DIMENSION_LABELS.get(name, name)
                try:
                    verdict = future.result(timeout=1)
                    verdicts.append(verdict)
                    logger.debug("质检维度 '%s'(%s) 完成: passed=%s", name, label, verdict.passed)
                except TimeoutError:
                    logger.warning("质检维度 '%s'(%s) 结果获取超时，已跳过", name, label)
                except Exception as exc:
                    logger.warning("质检维度 '%s'(%s) 异常，已跳过: %s", name, label, exc)

            # 超时的 futures
            for future in not_done:
                name = future_to_name[future]
                label = _DIMENSION_LABELS.get(name, name)
                logger.warning(
                    "质检维度 '%s'(%s) 超时(%ds)，已跳过",
                    name, label, total_timeout,
                )

    def _run_sequential(
        self,
        checkers: dict[str, QualityJudge],
        query: str,
        answer: str,
        context: str,
        verdicts: list[QualityVerdict],
        sources: list | None = None,
        **kwargs,
    ) -> None:
        """串行执行多个 LLM 质检维度的 evaluate() 方法。

        Args:
            checkers: 需要串行执行的质检器字典。
            query: 用户问题。
            answer: 模型回答。
            context: 检索上下文。
            verdicts: 用于收集评估结果的列表（原地追加）。
            sources: 来源列表（透传至支持 sources 的 checker，如 VectorFactualityChecker）。
        """
        for name, checker in checkers.items():
            label = _DIMENSION_LABELS.get(name, name)
            try:
                verdict = checker.evaluate(query, answer, context, sources=sources, **kwargs)
                verdicts.append(verdict)
                logger.debug("质检维度 '%s'(%s) 完成: passed=%s", name, label, verdict.passed)
            except Exception as exc:
                logger.warning("质检维度 '%s'(%s) 异常，已跳过: %s", name, label, exc)

    # ── 工具方法 ─────────────────────────────────────────

    @staticmethod
    def _extract_scores(sources: list) -> list[float]:
        """从 sources 列表中提取数值分数。

        支持两种格式：
        - Pydantic 模型（有 ``.score`` 属性）
        - 普通 dict（有 ``"score"`` 键）

        自动过滤 ``None`` 和不可转换为 float 的值。

        Args:
            sources: 来源列表。

        Returns:
            list[float]: 有效的分数列表。空列表表示无可用分数数据。
        """
        scores: list[float] = []
        for s in sources:
            try:
                if hasattr(s, "score"):
                    val = s.score
                elif isinstance(s, dict):
                    val = s.get("score")
                else:
                    continue

                if val is not None:
                    scores.append(float(val))
            except (TypeError, ValueError):
                continue
        return scores
