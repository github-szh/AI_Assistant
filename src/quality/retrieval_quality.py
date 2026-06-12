"""检索质量检查器 — 纯数值计算，零 LLM 调用。

提供 RetrievalQualityChecker 类，在 LLM 生成前和后对检索分数进行量化评估。
所有计算均为同步、零延迟，不依赖任何 LLM 调用。
"""

from __future__ import annotations

from statistics import mean

from src.api.schemas import QualityVerdict


class RetrievalQualityChecker:
    """检索质量检查器：基于检索分数（cosine similarity）的纯数值评估。

    在 LLM 生成前调用（预生成检查 — should_skip_llm）和
    生成后报告（生成后评估 — evaluate）。

    用法::

        checker = RetrievalQualityChecker()

        # 预生成检查（零延迟）
        if checker.should_skip_llm(scores, threshold=0.65):
            return "未找到相关上下文，无法生成回答"

        # 生成后评估
        verdict = checker.evaluate(query, scores)
        print(verdict.passed, verdict.score, verdict.details)
    """

    # 默认边界线阈值：低于此值的平均分虽然 passed=True 但标记为 borderline
    _BORDERLINE_THRESHOLD: float = 0.5
    # 失败阈值：平均分低于此值判定为 passed=False
    _FAIL_THRESHOLD: float = 0.1  # BGE 归一化后最低分恒为 0.0，阈值不宜过高
    # 阈值通过率计算中使用的相关度基线
    _RELEVANCE_BASELINE: float = 0.3

    def evaluate(
        self, query: str, retrieved_nodes_scores: list[float]
    ) -> QualityVerdict:
        """对检索分数进行全面质量评估。

        计算四个维度：
          - **平均分**（avg_score）：总体质量水平
          - **最高分**（max_score）：最佳匹配质量
          - **阈值通过率**（pass_rate）：超过相关度基线（0.3）的结果比例
          - **分数离散度**（dispersion）：质量均匀程度（max - min）

        评分规则：
          - avg >= 0.5 → passed=True, score=avg
          - 0.3 <= avg < 0.5 → passed=True, score=avg（记录为 borderline，details 含提示）
          - avg < 0.3 → passed=False, score=avg

        Args:
            query: 原始查询字符串（用于记录上下文，暂未写入 details 但保留接口扩展）
            retrieved_nodes_scores: 检索结果相似度分数列表，范围 [0, 1]

        Returns:
            QualityVerdict: 质检判定结果
                - dimension="retrieval_quality"
                - score=平均分
                - passed=是否通过
                - details=四个维度的详细报告
        """
        # ── 空列表处理 ──
        if not retrieved_nodes_scores:
            return QualityVerdict(
                dimension="retrieval_quality",
                passed=False,
                score=0.0,
                details="检索结果为空，无法评估检索质量",
            )

        # ── 计算各维度指标 ──
        avg_score = mean(retrieved_nodes_scores)
        max_score = max(retrieved_nodes_scores)
        min_score = min(retrieved_nodes_scores)
        dispersion = max_score - min_score
        pass_rate = (
            sum(1 for s in retrieved_nodes_scores if s > self._RELEVANCE_BASELINE)
            / len(retrieved_nodes_scores)
        )

        # ── 评分判定 ──
        if avg_score >= self._BORDERLINE_THRESHOLD:
            passed = True
            borderline = False
        elif avg_score >= self._FAIL_THRESHOLD:
            passed = True
            borderline = True
        else:
            passed = False
            borderline = False

        # ── 构造详细报告 ──
        details = (
            f"平均分={avg_score:.4f}, 最高分={max_score:.4f}, "
            f"最低分={min_score:.4f}, 分数离散度={dispersion:.4f}, "
            f"阈值通过率={pass_rate:.2%}, 共 {len(retrieved_nodes_scores)} 条检索结果"
        )
        if borderline:
            details += " （检索质量处于边界线，建议检查查询或知识库索引）"

        return QualityVerdict(
            dimension="retrieval_quality",
            passed=passed,
            score=round(avg_score, 4),
            details=details,
        )

    @staticmethod
    def should_skip_llm(
        scores: list[float], threshold: float = 0.65
    ) -> bool:
        """预生成检查：判断是否应跳过 LLM 生成。

        当所有检索分数均低于 *threshold* 时，说明没有找到有效上下文，
        建议跳过 LLM 生成（避免基于噪声产生幻觉），直接返回"无结果"提示。

        此方法是同步的，零延迟 —— 在 LLM 调用前执行，不产生额外成本。

        Args:
            scores: 检索结果相似度分数列表。
            threshold: 分数阈值，低于此值的最高分视为"无有效结果"。
                      默认 0.65 对应 ``src.config.settings.retrieval_stage1_threshold``。

        Returns:
            True → 应跳过 LLM 生成（最高分 < threshold）
            False → 继续 LLM 生成（存在有效结果）
        """
        if not scores:
            return True
        return max(scores) < threshold
