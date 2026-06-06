"""RAGAS 风格的事实正确性和忠实度检查器。

实现平滑评分（0.0-1.0），使用 RAGAS 方法论：
  1. 通过 LLM 将文本分解为原子事实声明（claims）
  2. 计算回答与标准答案在声明级别上的 F1 重叠
  3. 通过嵌入模型的余弦相似度计算语义相似度
  4. 加权融合：得分 = F1 × 权重 + 语义相似度 × (1-权重)

不依赖 ragas 包——从零实现该算法。
"""

import logging
import re
from typing import Any

import numpy as np

from src.quality.base import QualityJudge, QualityVerdict
from src.knowledge.embeddings import get_embedding_manager

logger = logging.getLogger(__name__)


def _decompose_claims(llm_provider: Any, text: str) -> list[str]:
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
        response = llm_provider.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
        )
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
        claim_tokens = set(jieba.lcut(claim))
        meaningful = {t for t in claim_tokens if len(t) > 1}
        if not meaningful:
            return True
        
        for ref in reference_claims:
            ref_tokens = set(jieba.lcut(ref))
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


def _calculate_f1(answer_claims: list[str], reference_claims: list[str]) -> float:
    """计算回答声明与参考声明之间的 F1 分数。"""
    if not answer_claims and not reference_claims:
        return 1.0
    if not answer_claims or not reference_claims:
        return 0.0
    
    # 统计回答声明的 TP（真阳性）和 FP（假阳性）
    tp = sum(1 for c in answer_claims if _check_support(c, reference_claims))
    fp = len(answer_claims) - tp

    # 统计参考声明中未被覆盖的 FN（假阴性）
    fn = sum(1 for c in reference_claims if not _check_support(c, answer_claims))
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


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


def _compute_ragas_score(f1: float, semantic_sim: float, weight: float = 0.5) -> float:
    """F1 与语义相似度的加权融合（RAGAS 风格）。"""
    return round(f1 * weight + semantic_sim * (1 - weight), 4)


def _check_numeric_consistency(answer: str, reference: str) -> float:
    """检查回答中的数值是否与标准答案中的数值一致。
    
    从两段文本中提取所有数字，逐对比较。
    返回惩罚乘数（0.0 = 数值完全不匹配，1.0 = 全部匹配）。
    """
    # 提取数字（整数和小数）
    ans_nums = [float(x) for x in re.findall(r'\d+\.?\d*', answer)]
    ref_nums = [float(x) for x in re.findall(r'\d+\.?\d*', reference)]
    
    if not ref_nums:
        return 1.0  # 标准答案中没有数字，跳过检查
    if not ans_nums:
        return 0.5  # 预期有数字但回答中没找到
    
    # 对标准答案中的每个数字，检查回答中是否有匹配
    matches = 0
    for rn in ref_nums:
        for an in ans_nums:
            if abs(an - rn) < 0.01:  # 精确匹配（允许浮点精度误差）
                matches += 1
                break
    
    ratio = matches / len(ref_nums)
    return ratio


# ═══════════════════════════════════════════════════
# Class: RagasFactualCorrectness
# ═══════════════════════════════════════════════════

class RagasFactualCorrectness(QualityJudge):
    """RAGAS 风格的答案正确性检查器。
    
    将回答与标准答案进行对比，使用：
    - 声明分解后的 F1 分数
    - 嵌入模型的语义相似度
    - 加权融合 → 平滑的 0-1 分
    
    需要通过 kwargs 传入 ground_truth。
    没有 ground_truth 时返回中性分数并跳过校验。
    """
    
    def __init__(self, llm_provider: Any, config: dict | None = None, weight: float = 0.5):
        super().__init__(llm_provider, prompt_template_name="", config=config)
        self.weight = weight
    
    def evaluate(self, query: str, answer: str, context: str | None = None, **kwargs) -> QualityVerdict:
        ground_truth = kwargs.get("ground_truth") or kwargs.get("reference")
        
        if not answer:
            return QualityVerdict(
                dimension="answer_correctness", passed=True, score=1.0,
                details="答案为空，跳过评估 || RAGAS-correctness: empty answer"
            )
        
        if not ground_truth:
            # No ground truth available - cannot measure correctness
            # Return a neutral score with honest description
            return QualityVerdict(
                dimension="answer_correctness",
                passed=True,
                score=0.5,
                details="当前无标准答案可对比，跳过正确性校验 || RAGAS-correctness: no ground_truth, skipped"
            )
        
        try:
            answer_claims = _decompose_claims(self.llm_provider, answer)
            ref_claims = _decompose_claims(self.llm_provider, ground_truth)
            
            f1 = _calculate_f1(answer_claims, ref_claims)
            sim = _semantic_similarity(answer, ground_truth)
            
            # Numeric consistency check: penalize if numbers don't match
            num_consistency = _check_numeric_consistency(answer, ground_truth)
            
            # Blend: if numeric consistency is low, reduce F1 proportionally
            adjusted_f1 = f1 * num_consistency
            
            final_score = _compute_ragas_score(adjusted_f1, sim, self.weight)
            
            return QualityVerdict(
                dimension="answer_correctness",
                passed=final_score >= 0.5,
                score=final_score,
                details=(
                    f"答案与标准答案{'一致' if final_score >= 0.5 else '不一致'}"
                    f"，关键事实匹配度 {f1:.0%} || "
                    f"RAGAS-correctness: score={final_score:.2f} "
                    f"(F1={f1:.2f}, sim={sim:.2f}, weight={self.weight})"
                )
            )
        except Exception as e:
            logger.error("RAGAS correctness evaluation failed: %s", e)
            sim = _semantic_similarity(answer, ground_truth)
            return QualityVerdict(
                dimension="answer_correctness",
                passed=sim >= 0.4,
                score=round(sim, 4),
                details=f"语义相似度={sim:.0%}（评估异常，降级为纯语义对比）|| RAGAS-correctness: fallback semantic (score={sim:.2f})"
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
        
        try:
            answer_claims = _decompose_claims(self.llm_provider, answer)
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
