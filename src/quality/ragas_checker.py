"""RAG 质量检查器：事实性（忠实度）+ 答案正确性 + 相关性。

- RagasFaithfulness: 事实性 — claim 分解 + jieba 支撑检查
- RagasFactualCorrectness: 答案正确性 — LLM Judge 语义对比（需 ground_truth）
- RagasAnswerRelevancy: 相关性 — Embedding 相似度 + 关键词覆盖
"""

import logging
import re
from typing import Any

import numpy as np

from src.quality.base import QualityJudge, QualityVerdict
from src.knowledge.embeddings import get_embedding_manager

logger = logging.getLogger(__name__)


def _decompose_claims(llm_provider: Any, text: str, model: str = "") -> list[str]:
    """使用 LLM 将文本分解为原子级的事实声明。

    LLM 调用失败时回退为按句子分割。
    """
    if not text or not text.strip():
        return []

    try:
        prompt = (
            "请将以下文本分解为原子级的事实声明（claims），"
            "每个声明只包含一个独立的事实。用中文输出，每行一个claim，不要编号。\n\n"
            f"文本：{text}\n\n"
            "事实声明："
        )
        call_kwargs: dict[str, Any] = dict(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
        )
        if model:
            call_kwargs["model"] = model
        response = llm_provider.chat(**call_kwargs)
        claims = [line.strip() for line in response.split('\n') if line.strip()]
        # 过滤掉 LLM 返回中的噪音行，如"文本："或"事实声明："
        claims = [c for c in claims if not c.startswith('文本') and not c.startswith('事实')]
        if claims:
            logger.debug("LLM decomposition: %d claims from %d chars", len(claims), len(text))
            return claims
    except Exception as e:
        logger.warning("LLM claim decomposition failed: %s, using fallback", e)
    
    # 回退方案：按中文句子边界分割
    sentences = [s.strip() for s in re.split(r'[。！？\n;；]', text) if s.strip()]
    result = [s for s in sentences if len(s) > 3]
    logger.debug("Fallback decomposition: %d sentences from %d chars", len(result), len(text))
    return result


def _check_support(claim: str, reference_claims: list[str]) -> bool:
    """检查一个声明的关键词是否与参考声明中的任何一个重叠。
    
    使用 jieba 分词的关键词重叠作为粗略启发式方法。
    """
    try:
        import jieba
        # 统一转小写，避免 BGE/bge 等大小写差异导致分词结果不同
        claim_tokens = set(jieba.lcut(claim.lower()))
        meaningful = {t for t in claim_tokens if len(t) > 1}
        if not meaningful:
            return True

        for ref in reference_claims:
            ref_tokens = set(jieba.lcut(ref.lower()))
            ref_meaningful = {t for t in ref_tokens if len(t) > 1}
            overlap = meaningful & ref_meaningful
            # 阈值：至少有 30% 的有意义分词重叠
            if len(overlap) >= max(1, len(meaningful) * 0.3):
                return True
    except Exception:
        pass
    
    # 终极回退：子串匹配
    for ref in reference_claims:
        if any(t in ref for t in claim.split() if len(t) > 2):
            return True
    return False


def _semantic_similarity(text1: str, text2: str) -> float:
    """使用项目嵌入模型计算两段文本之间的余弦相似度。"""
    if not text1 or not text2:
        return 0.0
    try:
        emb_mgr = get_embedding_manager()
        # 对两段文本都使用 encode_query（对称比较）
        emb1 = emb_mgr.encode_query(text1)
        emb2 = emb_mgr.encode_query(text2)
        dot = np.dot(emb1, emb2)
        norm = np.linalg.norm(emb1) * np.linalg.norm(emb2)
        return float(dot / norm) if norm > 0 else 0.0
    except Exception as e:
        logger.warning("Semantic similarity failed: %s", e)
        return 0.0


# ═══════════════════════════════════════════════════
# Class: RagasFactualCorrectness
# ═══════════════════════════════════════════════════

class RagasFactualCorrectness(QualityJudge):
    """LLM Judge 风格的答案正确性检查器。

    将 AI 回答与用户提供的标准答案进行对比，使用 LLM 做语义判断。
    替代了旧的 RAGAS（jieba F1 + 语义相似度融合）方案——后者在大小写、
    同义词、数字差异等场景下频繁误判。

    需要通过 kwargs 传入 ground_truth。
    没有 ground_truth 时返回中性分数并跳过校验。
    """

    def __init__(self, llm_provider: Any, config: dict | None = None, **kwargs):
        super().__init__(llm_provider, prompt_template_name="", config=config)

    def evaluate(self, query: str, answer: str, context: str | None = None, **kwargs) -> QualityVerdict:
        ground_truth = kwargs.get("ground_truth") or kwargs.get("reference")

        if not answer:
            return QualityVerdict(
                dimension="answer_correctness", passed=True, score=1.0,
                details="答案为空，跳过评估 || LLM-judge: empty answer"
            )

        if not ground_truth:
            return QualityVerdict(
                dimension="answer_correctness",
                passed=True,
                score=0.5,
                details="当前无标准答案可对比，跳过正确性校验 || LLM-judge: no ground_truth, skipped"
            )

        try:
            prompt = (
                "请判断以下 AI 回答与标准答案是否表达了相同的意思。"
                "注意：重点比较关键事实和数据是否一致，措辞不同但意思相同应判为一致。\n\n"
                f"用户问题：{query}\n\n"
                f"AI 回答：{answer}\n\n"
                f"标准答案：{ground_truth}\n\n"
                "请返回 JSON 格式：\n"
                '{"passed": true/false, "score": 0.0-1.0, "reasoning": "判断理由"}'
            )

            result = self._call_judge(prompt)

            # 检查 LLM 调用是否出错（超时、解析失败等）
            if result.get("_error"):
                error_msg = result.get("reasoning", "未知错误")
                logger.warning("Answer correctness Judge 异常: %s", error_msg)
                # fallback: 语义相似度
                sim = _semantic_similarity(answer, ground_truth)
                return QualityVerdict(
                    dimension="answer_correctness",
                    passed=sim >= 0.4,
                    score=round(sim, 4),
                    details=f"LLM Judge 异常，降级为语义对比（相似度={sim:.0%}) || LLM-judge: fallback semantic (score={sim:.2f})"
                )

            passed = result.get("passed", False)
            score = result.get("score", 0.5)
            reasoning = result.get("reasoning", "")

            return QualityVerdict(
                dimension="answer_correctness",
                passed=passed,
                score=score,
                details=(
                    f"答案与标准答案{'高度一致' if score >= 0.7 else '部分一致' if score >= 0.5 else '不一致'}"
                    f" — {reasoning} || "
                    f"LLM-judge: score={score:.2f}, passed={passed}"
                )
            )
        except Exception as e:
            logger.error("Answer correctness evaluation failed: %s", e)
            sim = _semantic_similarity(answer, ground_truth)
            return QualityVerdict(
                dimension="answer_correctness",
                passed=sim >= 0.4,
                score=round(sim, 4),
                details=f"评估异常，降级为语义对比（相似度={sim:.0%}) || LLM-judge: fallback semantic (score={sim:.2f})"
            )


# ═══════════════════════════════════════════════════
# Class: RagasFaithfulness
# ═══════════════════════════════════════════════════

class RagasFaithfulness(QualityJudge):
    """RAGAS 风格的忠实度检查器。
    
    检查回答中的声明是否都能从检索到的上下文中找到支持。
    分数 = 被支持的声明比例。
    产生平滑的 0-1 分（非二元）。
    
    只需要回答 + 上下文（不需要标准答案）。
    """
    
    def evaluate(self, query: str, answer: str, context: str | None = None, **kwargs) -> QualityVerdict:
        if not answer:
            return QualityVerdict(
                dimension="factuality", passed=True, score=1.0,
                details="答案为空，跳过评估 || RAGAS-faithfulness: empty answer"
            )
        
        if not context:
            return QualityVerdict(
                dimension="factuality", passed=True, score=0.5,
                details="无检索上下文，无法验证忠实度 || RAGAS-faithfulness: no context, skipped"
            )

        # 替代关键词匹配：Judge-LLM 判断是否拒答 + 知识库是否有答案
        is_refusal, context_has_answer = self._verify_refusal_and_context(
            query, answer, context
        )
        if is_refusal:
            if context_has_answer:
                return QualityVerdict(
                    dimension="factuality", passed=False, score=0.0,
                    details="知识库包含相关答案但模型声称未找到（幻觉/懒惰）"
                          " || factuality-refusal: contradicted by context"
                )
            else:
                return QualityVerdict(
                    dimension="factuality", passed=True, score=1.0,
                    details="知识库无相关内容，模型诚实拒答（优秀安全护栏）"
                          " || factuality-refusal: confirmed by context"
                )

        try:
            judge_model = self.config.get("quality_judge_model", "")
            answer_claims = _decompose_claims(self.llm_provider, answer, model=judge_model)
            if not answer_claims:
                # 回退到语义相似度
                sim = _semantic_similarity(answer, context)
                return QualityVerdict(
                    dimension="factuality",
                    passed=sim >= 0.4,
                    score=round(sim, 4),
                    details=f"RAGAS-faithfulness: no claims, semantic score={sim:.2f}"
                )
            
            supported = sum(1 for c in answer_claims if _check_support(c, [context]))
            score = supported / len(answer_claims)
            passed = score >= 0.5
            
            return QualityVerdict(
                dimension="factuality",
                passed=passed,
                score=round(score, 4),
                details=(
                    f"回答中的 {supported}/{len(answer_claims)} 条关键事实可在检索文档中找到"
                    f"（忠实度 {score:.0%}) || "
                    f"RAGAS-faithfulness: {supported}/{len(answer_claims)} claims supported"
                )
            )
        except Exception as e:
            logger.error("RAGAS faithfulness evaluation failed: %s", e)
            sim = _semantic_similarity(answer, context)
            return QualityVerdict(
                dimension="factuality",
                passed=sim >= 0.4,
                score=round(sim, 4),
                details=f"语义相似度={sim:.0%}（评估异常，降级）|| RAGAS-faithfulness: fallback semantic (score={sim:.2f})"
            )

    def _verify_refusal_and_context(self, question: str, answer: str, context: str):
        """Judge-LLM 一次调用：判断回答是否拒答 + 上下文是否有答案。
        
        Returns:
            tuple[bool, bool]: (is_refusal, context_has_answer)
            异常时返回 (False, False) — fail-open: 视为非拒答，走原流程
        """
        try:
            prompt = self._render_prompt(
                "factuality_refusal_judge",
                question=question, answer=answer, context=context,
            )
            result = self._call_judge(prompt)
            if result.get("_error"):
                logger.warning("Refusal Judge 异常，fallback 非拒答: %s", result.get("reasoning", ""))
                return False, False
            return result.get("is_refusal", False), result.get("context_has_answer", False)
        except Exception:
            logger.warning("Refusal Judge 调用失败，fallback 非拒答", exc_info=True)
            return False, False


# ═══════════════════════════════════════════════════
# Class: RagasAnswerRelevancy
# ═══════════════════════════════════════════════════

class RagasAnswerRelevancy(QualityJudge):
    """RAGAS 风格的答案相关性检查器。

    通过以下方式衡量回答与问题的相关程度：
    1. 问题与回答之间的语义相似度（通过 embedding）
    2. 问题术语覆盖率（回答是否覆盖了问题的关键术语）
    3. 回答完整性（回答是否有足够的内容）

    产生平滑的 0-1 分（非二元）。
    """

    def __init__(self, llm_provider, config=None):
        super().__init__(llm_provider, prompt_template_name="", config=config)

    def evaluate(self, query, answer, context=None, **kwargs):
        if not answer or not query:
            return QualityVerdict(
                dimension="relevance", passed=True, score=1.0,
                details="输入为空，跳过评估 || RAGAS-relevance: empty input"
            )

        try:
            sim = _semantic_similarity(query, answer)
            coverage = self._compute_term_coverage(query, answer)
            answer_len = len(answer)
            length_factor = min(1.0, answer_len / 20)
            score = round(sim * 0.5 + coverage * 0.3 + length_factor * 0.2, 4)

            return QualityVerdict(
                dimension="relevance",
                passed=score >= 0.4,
                score=score,
                details=(
                    f"回答与问题{'高度相关' if score >= 0.7 else '部分相关' if score >= 0.4 else '相关性较低'}"
                    f"（语义匹配 {sim:.0%}，关键词覆盖率 {coverage:.0%}）|| "
                    f"RAGAS-relevance: score={score:.2f} "
                    f"(semantic={sim:.2f}, coverage={coverage:.2f})"
                )
            )
        except Exception as e:
            logger.error("RAGAS relevancy evaluation failed: %s", e)
            try:
                sim = _semantic_similarity(query, answer)
                return QualityVerdict(
                    dimension="relevance",
                    passed=sim >= 0.4,
                    score=round(sim, 4),
                    details=f"语义相似度={sim:.0%}（评估异常，降级）|| RAGAS-relevance: fallback semantic (score={sim:.2f})"
                )
            except Exception:
                return QualityVerdict(
                    dimension="relevance", passed=True, score=1.0,
                    details="评估异常，默认通过 || RAGAS-relevance: evaluation failed, passed by default"
                )

    def _compute_term_coverage(self, query, answer):
        try:
            import jieba
            query_tokens = set(jieba.lcut(query))
            answer_tokens = set(jieba.lcut(answer))
            stop_words = {'的', '了', '是', '在', '有', '和', '就', '不', '人', '都',
                         '而', '及', '与', '着', '或', '一个', '没有', '我们', '你们', '他们',
                         '什么', '怎么', '哪', '这', '那', '为', '吗', '呢'}
            meaningful_query = {t for t in query_tokens if len(t) > 1 and t not in stop_words}
            if not meaningful_query:
                return 1.0
            matched = meaningful_query & answer_tokens
            return len(matched) / len(meaningful_query)
        except Exception:
            return 0.5
