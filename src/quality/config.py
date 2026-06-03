"""Quality assurance configuration models and defaults.

提供干预规则（InterventionRule）、安全分类（SafetyCategory）、
关键词匹配（KeywordMatch）、质量阈值（QualityThresholds）等
Pydantic 模型，以及默认配置加载函数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ── 检索质量检查器配置 ────────────────────────────────

# 预生成检查默认阈值：低于此值的最高分视为"无有效结果"，跳过 LLM 生成。
# 对应 src.config.settings.retrieval_stage1_threshold（0.65）的默认值。
RETRIEVAL_QUALITY_SKIP_THRESHOLD: float = 0.65

# ── 干预规则 ─────────────────────────────────────────


class InterventionRule(BaseModel):
    """干预规则：定义当检测到特定违规类型时采取的行动。

    Attributes:
        violation_type: 违规类型标识符，如 "safety_harmful_content"、"factuality_hallucination"
        action: 采取的干预动作，支持：block / rewrite / warn / degrade / none
        message: 触发规则时的提示消息（日志记录或返回给用户）
        priority: 优先级（1 最高），用于规则冲突时的排序
    """

    violation_type: str = Field(
        ..., description="违规类型标识符，如 safety_harmful_content"
    )
    action: Literal["block", "rewrite", "warn", "degrade", "none"] = Field(
        ..., description="干预动作：block=拦截, rewrite=重写, warn=告警, degrade=降级, none=放行"
    )
    message: str = Field(
        default="", description="触发规则时的提示消息，用于日志记录或返回给用户"
    )
    priority: int = Field(
        default=99, ge=1, le=99, description="优先级（1 最高），用于规则冲突排序"
    )


# ── 安全分类 ─────────────────────────────────────────


class SafetyCategory(BaseModel):
    """安全分类：定义需要监控/拦截的内容类别。

    Attributes:
        name: 分类名称，如 "harmful_content"、"prompt_injection"
        keywords: 触发该分类的关键词列表（精确匹配）
        regex_patterns: 触发该分类的正则表达式模式列表
        description: 分类用途说明
    """

    name: str = Field(..., description="分类名称，如 harmful_content")
    keywords: list[str] = Field(
        default_factory=list, description="触发该分类的关键词列表（精确匹配）"
    )
    regex_patterns: list[str] = Field(
        default_factory=list, description="触发该分类的正则表达式模式列表"
    )
    description: str = Field(
        default="", description="分类用途说明"
    )


# ── 关键词匹配结果 ────────────────────────────────────


class KeywordMatch(BaseModel):
    """关键词匹配结果：记录匹配到的具体位置和类型。

    Attributes:
        category: 匹配到的安全分类名称
        keyword: 匹配到的具体关键词
        position: 匹配位置（字符偏移量）
        match_type: 匹配方式，如 "exact"（精确）、"regex"（正则）
    """

    category: str = Field(..., description="匹配到的安全分类名称")
    keyword: str = Field(..., description="匹配到的具体关键词或正则模式")
    position: int = Field(default=0, ge=0, description="匹配位置（字符偏移量）")
    match_type: Literal["exact", "regex"] = Field(
        default="exact", description="匹配方式：exact=精确匹配, regex=正则匹配"
    )


# ── 质量阈值 ─────────────────────────────────────────


class QualityThresholds(BaseModel):
    """检索质量阈值配置：定义各维度评估的阈值标准。

    Attributes:
        relevance_score_min: 相关性分数最低阈值（0~1），低于此值视为不相关
        context_coverage_min: 上下文覆盖率最低阈值（0~1），低于此值视为覆盖不足
        factuality_confidence_min: 事实性置信度最低阈值（0~1）
        retrieval_precision_min: 检索精确率最低阈值（0~1）
        retrieval_recall_min: 检索召回率最低阈值（0~1）
        max_hallucination_risk: 最大幻觉风险分数（0~1），超过此值需降级或拦截
    """

    relevance_score_min: float = Field(
        default=0.3, ge=0.0, le=1.0, description="相关性分数最低阈值（0~1），低于此值视为不相关"
    )
    context_coverage_min: float = Field(
        default=0.4, ge=0.0, le=1.0, description="上下文覆盖率最低阈值（0~1），低于此值视为覆盖不足"
    )
    factuality_confidence_min: float = Field(
        default=0.6, ge=0.0, le=1.0, description="事实性置信度最低阈值（0~1）"
    )
    retrieval_precision_min: float = Field(
        default=0.5, ge=0.0, le=1.0, description="检索精确率最低阈值（0~1）"
    )
    retrieval_recall_min: float = Field(
        default=0.5, ge=0.0, le=1.0, description="检索召回率最低阈值（0~1）"
    )
    max_hallucination_risk: float = Field(
        default=0.7, ge=0.0, le=1.0, description="最大幻觉风险分数（0~1），超过此值需降级或拦截"
    )


# ── 默认规则和分类 ────────────────────────────────────

# 支持的干预动作映射说明：
#   block   — 完全拦截，不返回任何内容
#   rewrite — 重写回答以移除违规内容
#   warn    — 仅记录警告日志，正常返回
#   degrade — 降级处理（如不提供来源引用，或给出免责声明）
#   none    — 放行，不采取任何干预


def get_default_intervention_rules() -> list[InterventionRule]:
    """返回默认干预规则列表。

    规则按优先级排序：
      - safety_*      → block（优先级 1）
      - factuality_*  → degrade（优先级 2）
      - retrieval_*   → warn（优先级 3）
      - relevance_*   → warn（优先级 4）
    """
    return [
        # ── 安全相关（最高优先级：拦截） ──
        InterventionRule(
            violation_type="safety_harmful_content",
            action="block",
            message="检测到有害内容，已拦截回答",
            priority=1,
        ),
        InterventionRule(
            violation_type="safety_prompt_injection",
            action="block",
            message="检测到提示注入攻击，已拦截回答",
            priority=1,
        ),
        InterventionRule(
            violation_type="safety_personal_info_leak",
            action="block",
            message="检测到个人信息泄露风险，已拦截回答",
            priority=1,
        ),
        InterventionRule(
            violation_type="safety_sensitive_topic",
            action="block",
            message="检测到敏感话题，已拦截回答",
            priority=1,
        ),
        # ── 事实性相关（降级） ──
        InterventionRule(
            violation_type="factuality_hallucination",
            action="degrade",
            message="检测到可能的幻觉，回答已降级处理",
            priority=2,
        ),
        InterventionRule(
            violation_type="factuality_contradiction",
            action="degrade",
            message="检测到事实矛盾，回答已降级处理",
            priority=2,
        ),
        InterventionRule(
            violation_type="factuality_source_mismatch",
            action="degrade",
            message="回答与来源内容不匹配，已降级处理",
            priority=2,
        ),
        # ── 检索质量相关（告警） ──
        InterventionRule(
            violation_type="retrieval_low_precision",
            action="warn",
            message="检索结果精确率偏低，请检查查询或索引",
            priority=3,
        ),
        InterventionRule(
            violation_type="retrieval_low_recall",
            action="warn",
            message="检索结果召回率偏低，建议补充知识库内容",
            priority=3,
        ),
        InterventionRule(
            violation_type="retrieval_no_results",
            action="warn",
            message="检索未返回有效结果",
            priority=3,
        ),
        # ── 相关性相关（告警） ──
        InterventionRule(
            violation_type="relevance_low_score",
            action="warn",
            message="上下文与问题的相关性不足",
            priority=4,
        ),
        InterventionRule(
            violation_type="relevance_off_topic",
            action="warn",
            message="回答偏离主题或与上下文不相关",
            priority=4,
        ),
        InterventionRule(
            violation_type="relevance_incomplete_coverage",
            action="warn",
            message="上下文覆盖不完整，可能无法完整回答问题",
            priority=4,
        ),
    ]


def get_default_safety_categories() -> list[SafetyCategory]:
    """返回内置安全类别列表。

    包含以下类别（关键词占位，Task 8 会完善）：
      - harmful_content:   有害内容
      - prompt_injection:  提示注入
      - personal_info:     个人信息泄露
      - sensitive_topic:   敏感话题
      - illegal_content:   违法内容
      - misinformation:    虚假信息
    """
    return [
        # ── 有害内容（8+ 关键词） ──
        SafetyCategory(
            name="harmful_content",
            keywords=[
                "暴力", "色情", "仇恨", "歧视", "自残", "自杀",
                "恐怖主义", "校园暴力", "虐待", "血腥",
            ],
            regex_patterns=[],
            description="有害内容：包含暴力、色情、仇恨言论、恐怖主义等",
        ),
        # ── 提示注入（6+ 关键词 + 2 条正则） ──
        SafetyCategory(
            name="prompt_injection",
            keywords=[
                "忽略指令", "忘记之前", "system prompt", "忽略以上",
                "无视规则", "突破限制", "模拟对话",
            ],
            regex_patterns=[
                # 英文注入模式：ignore/forget/disregard + above/previous/instructions
                r"(?i)(ignore|forget|disregard)\s+(all\s+)?(above|previous|instructions)",
                # 中文注入模式：不要遵循 + 之前的规则/指令
                r"(?i)(不要|无需|不必)(遵循|遵守|理会)(.*?)(规则|指令|限制)",
            ],
            description="提示注入：试图绕过系统指令的恶意输入",
        ),
        # ── 个人信息泄露（6+ 关键词 + 身份证/手机号正则） ──
        SafetyCategory(
            name="personal_info",
            keywords=[
                "身份证", "手机号", "银行卡", "密码", "住址", "社保号",
                "护照号", "信用卡",
            ],
            regex_patterns=[
                # 大陆 18 位身份证号（17 位数字 + 1 位校验码[数字或X]）
                # 使用 (?<!\d)/(?!\d) 替代 \b，因为 Python 3 中中文汉字被视为 \w
                r"(?<!\d)\d{17}[\dXx](?!\d)",
                # 中国大陆手机号（11 位，1 开头）
                r"(?<!\d)1[3-9]\d{9}(?!\d)",
                # 邮箱地址
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            ],
            description="个人信息泄露：包含身份证号、手机号、银行卡号、邮箱等敏感信息",
        ),
        # ── 敏感话题（6+ 关键词） ──
        SafetyCategory(
            name="sensitive_topic",
            keywords=[
                "政治敏感", "宗教极端", "领土争端", "历史虚无主义",
                "分裂言论", "种族歧视", "邪教组织",
            ],
            regex_patterns=[],
            description="敏感话题：涉及政治、宗教、领土、种族等争议性内容",
        ),
        # ── 违法内容（6+ 关键词） ──
        SafetyCategory(
            name="illegal_content",
            keywords=[
                "毒品", "赌博", "枪支", "爆炸物", "黑客", "钓鱼",
                "洗钱", "走私", "炸弹", "入侵", "作弊", "威胁", "恐吓",
            ],
            regex_patterns=[],
            description="违法内容：涉及毒品、赌博、枪支、黑客、洗钱等非法活动",
        ),
        # ── 虚假信息（6+ 关键词） ──
        SafetyCategory(
            name="misinformation",
            keywords=[
                "谣言", "阴谋论", "伪科学", "虚假新闻",
                "编造事实", "误导信息", "深度伪造",
            ],
            regex_patterns=[],
            description="虚假信息：包含谣言、阴谋论、伪科学、深度伪造等不实内容",
        ),
    ]


def load_safety_categories_from_yaml(path: str) -> list[SafetyCategory]:
    """从 YAML 文件加载自定义安全分类。

    文件格式示例：
        safety_categories:
          - name: custom_category
            keywords: ["keyword1", "keyword2"]
            regex_patterns: []
            description: "自定义分类说明"

    Args:
        path: YAML 文件路径

    Returns:
        list[SafetyCategory]: 解析后的安全分类列表

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: YAML 格式不符合预期
    """
    try:
        import yaml
    except ImportError:
        raise ImportError("需要安装 PyYAML：pip install pyyaml")

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "safety_categories" not in data:
        raise ValueError("YAML 文件必须包含顶层的 'safety_categories' 键")

    categories = data["safety_categories"]
    if not isinstance(categories, list):
        raise ValueError("'safety_categories' 必须是一个列表")

    return [SafetyCategory(**cat) for cat in categories]
