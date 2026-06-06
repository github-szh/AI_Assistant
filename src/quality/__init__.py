"""Quality assurance module for RAG pipeline.

提供干预规则、安全分类、质量阈值等配置模型，以及默认规则/分类的加载函数。
包含 QualityJudge 抽象基类、SafetyChecker 安全检查和 KeywordFilter 关键词过滤。
"""

from src.quality.base import QualityJudge, QualityVerdict
from src.quality.config import (
    InterventionRule,
    KeywordMatch,
    QualityThresholds,
    SafetyCategory,
    get_default_intervention_rules,
    get_default_safety_categories,
    load_safety_categories_from_yaml,
)
from src.quality.factuality import FactualityChecker
from src.quality.vector_factuality import VectorFactualityChecker
from src.quality.guard import QualityGuard
from src.quality.intervention import InterventionEngine
from src.quality.keyword_filter import KeywordFilter
from src.quality.relevance import RelevanceChecker
from src.quality.retrieval_quality import RetrievalQualityChecker
from src.quality.safety import SafetyChecker

__all__ = [
    # 基类
    "QualityJudge",
    "QualityVerdict",
    # 编排类
    "QualityGuard",
    # 检索质量检查器
    "RetrievalQualityChecker",
    # 检查器
    "SafetyChecker",
    "FactualityChecker",
    "VectorFactualityChecker",
    "RelevanceChecker",
    # 干预引擎
    "InterventionEngine",
    "KeywordFilter",
    # 配置模型
    "InterventionRule",
    "SafetyCategory",
    "KeywordMatch",
    "QualityThresholds",
    # 工具函数
    "get_default_intervention_rules",
    "get_default_safety_categories",
    "load_safety_categories_from_yaml",
]
