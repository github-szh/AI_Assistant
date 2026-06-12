# RAG Retrieval Quality Checker

> 检索质量检查器文档。
> 定义位置：`src/quality/retrieval_quality.py`
> 版本：v1.0

---

## 概述

**RetrievalQualityChecker** 是一个纯数值计算的检索质量评估工具，在 LLM 生成前（预生成检查）和生成后（质量报告）两个阶段使用。所有计算均为同步、零延迟，不依赖任何 LLM 调用。

### 设计原则

| 原则 | 说明 |
|------|------|
| 零 LLM 调用 | 仅对检索分数（cosine similarity）做数学统计 |
| 零延迟 | 所有方法均为同步，不发起任何网络请求 |
| 不修改检索逻辑 | 只读取分数，不介入检索流程 |

---

## 类结构

```text
RetrievalQualityChecker
├── evaluate()           → QualityVerdict   # 生成后评估
└── should_skip_llm()    → bool             # 预生成检查（静态方法）
```

---

## evaluate() — 生成后评估

对检索分数进行四个维度的量化分析。

### 计算维度

| 维度 | 公式 | 含义 |
|------|------|------|
| **平均分** | `sum(scores) / len(scores)` | 总体检索质量水平 |
| **最高分** | `max(scores)` | 最佳匹配块的质量 |
| **阈值通过率** | `count(s > 0.3) / total` | 超过相关度基线的结果比例 |
| **分数离散度** | `max - min` | 检索质量均匀程度（越大越不均匀） |

### 评分规则

| 平均分范围 | passed | 备注 |
|-----------|--------|------|
| `>= 0.5` | `True` | 正常通过 |
| `>= 0.3` 且 `< 0.5` | `True` | **borderline**，details 含提示 |
| `< 0.3` | `False` | 检索质量差 |

### 返回值示例

```python
# 高质量检索
QualityVerdict(
    dimension="retrieval_quality",
    passed=True,
    score=0.7000,
    details="平均分=0.7000, 最高分=0.9000, 最低分=0.5000, 分数离散度=0.4000, 阈值通过率=100.00%, 共 3 条检索结果"
)

# 低质量检索
QualityVerdict(
    dimension="retrieval_quality",
    passed=False,
    score=0.1500,
    details="平均分=0.1500, 最高分=0.2000, 最低分=0.1000, 分数离散度=0.1000, 阈值通过率=0.00%, 共 2 条检索结果"
)

# 空检索
QualityVerdict(
    dimension="retrieval_quality",
    passed=False,
    score=0.0,
    details="检索结果为空，无法评估检索质量"
)
```

---

## should_skip_llm() — 预生成检查

在 LLM 调用前快速判断是否应跳过生成流程。当所有检索分数均低于阈值时，说明没有找到有效上下文，继续 LLM 生成可能导致基于噪声的幻觉。

### 行为

| 条件 | 返回值 | 含义 |
|------|--------|------|
| `max(scores) < threshold` | `True` | 跳过 LLM 生成 |
| `max(scores) >= threshold` | `False` | 继续 LLM 生成 |
| 空列表 | `True` | 跳过 LLM 生成 |

### 默认阈值

`threshold` 默认值 0.65，对应 `src.config.settings.retrieval_stage1_threshold`，
也对应 `src.quality.config.RETRIEVAL_QUALITY_SKIP_THRESHOLD`。

---

## 使用示例

```python
from src.quality.retrieval_quality import RetrievalQualityChecker

checker = RetrievalQualityChecker()
scores = [0.92, 0.78, 0.45, 0.30]

# 1. 预生成检查（零延迟）
if checker.should_skip_llm(scores, threshold=0.65):
    # 最高分 = 0.92 >= 0.65，不会走到这里
    return "未找到相关上下文"

# 2. LLM 生成回答...

# 3. 生成后评估
verdict = checker.evaluate("用户问题", scores)
print(f"通过: {verdict.passed}, 得分: {verdict.score}")
print(f"详情: {verdict.details}")
```

---

## 在质检流水线中的位置

```text
用户 Query
    │
    ▼
QueryEngine._retrieve()
    │
    ▼
检索结果分数列表 [0.92, 0.45, ...]
    │
    ├──▶ should_skip_llm()  ──▶ True ──▶ 直接返回"无结果"
    │                                  （跳过 LLM，避免幻觉）
    │
    ▼
LLM 生成回答
    │
    ▼
evaluate()  ← 质量报告，非阻塞
    │
    ▼
InterventionEngine 综合判断
```

---

## 配置

配置项定义在 `src/quality/config.py`：

| 常量名 | 默认值 | 说明 |
|--------|--------|------|
| `RETRIEVAL_QUALITY_SKIP_THRESHOLD` | `0.65` | `should_skip_llm()` 默认阈值 |

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 RetrievalQualityChecker 类 |
