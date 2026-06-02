# RAG 回答相关性评估（Relevance Evaluation）

## 概述

回答相关性评估用于判断模型生成的回答是否直接回应了用户的问题。它是 RAG 质量评估体系中最基础的维度——如果回答离题，即使事实正确也没有意义。

**核心原则：相关性评估只看"回答 vs 问题"，不看"回答 vs 上下文"。**

## RelevanceChecker 设计

### 类层次

```
QualityJudge (ABC)          ← src/quality/base.py
  └── RelevanceChecker       ← src/quality/relevance.py
  └── FactualityChecker      (后续实现)
  └── SafetyChecker          (后续实现)
```

### 评估维度

RelevanceChecker 委托 LLM Judge 依据 `prompts/quality/relevance_judge.yaml` 从三个维度审查：

| 维度 | 说明 | 示例 |
|------|------|------|
| 是否直接回应问题 | 回答是否针对用户问题的核心意图 | 问"RAG 是什么"答"RAG 是一种检索增强生成技术"→ 通过 |
| 是否有离题/无关内容 | 回答中是否有与问题无关的长篇论述 | 问"今天天气"答"相对论"→ 不通过 |
| 是否有遗漏问题部分 | 多子问题是否都得到回答 | 问"优缺点"只答优点 → 部分通过 |

### 特殊处理

| 场景 | 处理方式 | 原因 |
|------|----------|------|
| "我不知道"/"无法回答" | 视为 **relevant** | LLM 正确回应了问题（表示无法回答），比胡编乱造更好 |
| LLM 调用异常（超时/网络错误） | **fail-open**：放行 + 日志警告 | 相关性是"质量提升"维度，不应阻断正常回答流程 |

### 配置参数

```python
RelevanceChecker(
    llm_provider=my_llm,       # 实现了 chat() 接口的 LLM 实例
    prompt_dir="prompts/quality",  # Prompt 模板目录
    judge_model=None,          # 评估模型名（None=使用 llm_provider 默认）
    threshold=0.7,             # 相关性阈值（低于此值日志告警）
)
```

### 返回值

```python
@dataclass
class QualityVerdict:
    passed: bool              # True=通过, False=不通过
    score: float              # 0.0(完全不相关) ~ 1.0(完全相关)
    reasoning: str            # LLM Judge 的评估理由
    metadata: dict            # 额外数据（RelevanceChecker 通常为空）
```

## 使用示例

```python
from src.quality.relevance import RelevanceChecker

# 初始化（使用真实 LLM Provider）
checker = RelevanceChecker(llm_provider=my_llm)

# 评估
verdict = checker.evaluate(
    query="什么是 RAG 技术？",
    answer="RAG（Retrieval-Augmented Generation）是检索增强生成技术。",
)

if verdict.passed:
    print(f"相关，分数: {verdict.score:.2f}")
    print(f"理由: {verdict.reasoning}")
else:
    print(f"不相关，分数: {verdict.score:.2f}")
    print(f"理由: {verdict.reasoning}")
```

## 测试覆盖

| 测试用例 | 验证点 | 场景 |
|----------|--------|------|
| `test_relevant_answer` | passed=True, score>=0.7 | 正常相关回答 |
| `test_off_topic_answer` | passed=False | 完全离题回答 |
| `test_partially_relevant` | 0.3 < score < 0.7 | 部分覆盖问题 |
| `test_idk_answer_relevant` | passed=True, score>=0.7 | "我不知道"正确拒答 |
| `test_judge_failure_fallback` | passed=True, "自动放行" | LLM 调用超时 |
| `test_empty_answer` | 不抛异常 | 空回答边界 |
| `test_long_query_answer` | 不抛异常 | 长文本边界 |
| `test_threshold_configuration` | 自定义阈值生效 | 配置验证 |

## Fail-Open 策略说明

RelevanceChecker 采用 **fail-open**（失败时放行）策略：

```
LLM 调用成功 → 返回 LLM Judge 的评估结果
LLM 调用失败 → 记录警告日志，返回默认通过结果（score=0.7）
```

设计决策记录在 `.sisyphus/notepads/rag-quality-assurance/decisions.md`。

## 与其他评估器的关系

```
RelevanceChecker  →  回答是否回答了问题（不看上下文）
FactualityChecker →  回答是否基于检索内容（依赖上下文）
SafetyChecker     →  回答是否安全合规（关键维度，fail-closed）
```

RelevanceChecker 通常最先执行，因为：
1. 如果回答离题，后续的事实性和安全性评估没有意义
2. 它的评估成本最低（不需要加载和传递 context）
3. 可以快速过滤明显不相关的回答，节省后续评估的 LLM 调用
