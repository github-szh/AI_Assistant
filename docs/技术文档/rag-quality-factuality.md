# 事实一致性检查（Factuality Checker）— RAGAS 版

> 检测 LLM 回答是否基于检索上下文，并与标准答案对比验证正确性。
> 采用 RAGAS 方法论实现平滑评分（0.0~1.0），取代原有的二元 LLM 评判。
>
> 实现位置：`src/quality/ragas_checker.py`
> 版本：v2.0（RAGAS）

---

## 概述

RAGAS 风格的事实性检查包含**两个维度**：

| 维度 | 类名 | 原方案 | 作用 |
|------|------|--------|------|
| **事实性 (factuality)** | `RagasFaithfulness` | FactualityChecker | 回答是否忠于检索到的上下文 |
| **答案正确性 (answer_correctness)** | `RagasFactualCorrectness` | ❌ 不存在（新增） | 回答是否与标准答案一致 |

### 核心变化

| 对比项 | 旧版 (v1.0) | 新版 (v2.0 RAGAS) |
|--------|------------|-------------------|
| 评分方式 | LLM 二元判定（0 或 100） | 声明分解 + F1 + 语义相似度 → **平滑 0~1** |
| 需要参数 | answer + context | answer + context（事实性）/ + ground_truth（答案正确性） |
| 输出 | passed=True/False | 平滑分数（0.0~1.0）+ 通过/不通过 |
| 可读性 | LLM 返回的 JSON | 中文可读描述 \|\| 技术数据 |

---

## 算法原理

### 核心公式

```text
最终得分 = F1 × 权重(0.5) + 语义相似度 × (1-权重)
```

### 三步计算

#### 1. 声明分解（Claim Decomposition）

将回答和标准答案拆解为原子事实声明：

```text
输入文本："系统使用PostgreSQL 15，Redis 6用于缓存"
分解结果：
  claim 1: "数据库使用PostgreSQL 15"
  claim 2: "Redis 6用于缓存"
```

**实现**：优先用 LLM 分解（DeepSeek），失败时回退到中文句子分割。
**源码**：`_decompose_claims()` — ragas_checker.py:24-57

#### 2. F1 分数计算

对比两个声明集合：

```text
回答声明: ["内存4GB", "磁盘20GB"]
标准声明: ["内存8GB", "磁盘50GB"]

TP(都有)=0, FP(回答有标准无)=2, FN(标准有回答无)=2
F1 = 2 × 0 / (2 × 0 + 2 + 2) = 0.0
```

**关键词匹配**：jieba 分词，30% 以上有意义分词重叠即视为匹配。
**源码**：`_check_support()` + `_calculate_f1()` — ragas_checker.py:60-108

#### 3. 语义相似度

```text
回答: "内存4GB"
标准: "内存8GB"
→ 余弦相似度 ≈ 0.85（语义接近，仅数值不同）
```

**源码**：`_semantic_similarity()` — ragas_checker.py:111-125

### 数值一致性检查

解决"top_k=30 vs top_k=20"这类语义相似但数值不同的问题：

```text
回答提取数字: [30]
标准提取数字: [20, 5]
30 vs 20 → 不匹配
数值一致性 = 0/2 = 0.0
调整后 F1 = 原F1 × 0.0 = 0.0
```

**源码**：`_check_numeric_consistency()` — ragas_checker.py:133-157

---

## RagasFaithfulness（事实性）

### 作用

检查回答中的每一个事实声明是否都能在检索到的上下文中找到支持。
替代了原有的 `FactualityChecker`（LLM 交叉评判）。

### 执行流程

```text
evaluate(query, answer, context)
    │
    ├─ Step 1: 空回答 → 跳过，score=1.0
    │
    ├─ Step 2: 无上下文 → 无法验证，score=0.5
    │
    ├─ Step 3: 声明分解回答 (claim decomposition)
    │
    ├─ Step 4: 无声明 → 回退到语义相似度
    │
    ├─ Step 5: 逐一检查每个 claim 是否在 context 中出现
    │
    ├─ Step 6: score = 被支持的 claims / 总 claims
    │
    └─ 异常 → 回退到语义相似度
```

### 分数示例

| 场景 | 回答 | 上下文 | 分数 |
|------|------|--------|------|
| 忠实引用 | "内存4GB" | 文档写"内存4GB" | 0.85 |
| 部分编造 | "内存8GB" | 文档写"内存4GB" | 0.33 |
| 完全编造 | "使用MongoDB" | 文档只提PostgreSQL | 0.0 |

### 源码

```python
class RagasFaithfulness(QualityJudge):
    """RAGAS 风格的忠实度检查器。"""
    # ragas_checker.py:240-297
```

---

## RagasFactualCorrectness（答案正确性）— 新增

### 作用

将回答与标准答案（ground_truth）对比，验证回答在事实上是否正确。
这是整个系统中**唯一能检测"引用噪声文档导致答案错误"的维度**。

### 执行流程

```text
evaluate(query, answer, context, ground_truth)
    │
    ├─ Step 1: 空回答 → 跳过
    │
    ├─ Step 2: 无 ground_truth → 跳过，score=0.5
    │          （生产环境随意提问时走此分支）
    │
    ├─ Step 3: 有 ground_truth → 执行 RAGAS 算法
    │
    ├─ Step 4: 声明分解回答和标准答案
    │
    ├─ Step 5: F1 = 计算声明重叠
    │
    ├─ Step 6: 语义相似度 = embedding 余弦
    │
    ├─ Step 7: 数值一致性检查
    │
    ├─ Step 8: adjusted_F1 = F1 × 数值一致性
    │
    ├─ Step 9: 最终分 = adjusted_F1 × 0.5 + 相似度 × 0.5
    │
    └─ 异常 → 回退到纯语义相似度
```

### 什么时候生效

| 场景 | ground_truth | 行为 | 显示 |
|------|-------------|------|------|
| 前端随意提问 | 无 | 跳过，score=0.5 | "跳过正确性校验" |
| 测评数据集 | 有（从eval_dataset传入） | 正常计算 | "一致/不一致" |
| 前端填了标准答案框 | 有（用户手动输入） | 正常计算 | "一致/不一致" |

### 源码

```python
class RagasFactualCorrectness(QualityJudge):
    """RAGAS 风格的答案正确性检查器。"""
    # ragas_checker.py:164-233
```

---

## 配置参数

在 `src/knowledge/query_engine.py` 中初始化：

```python
from src.quality.ragas_checker import RagasFaithfulness, RagasFactualCorrectness

ragas_faithfulness = RagasFaithfulness(llm_provider=llm)
ragas_correctness = RagasFactualCorrectness(llm_provider=llm, weight=0.5)

checkers = {
    "safety": SafetyChecker(llm_provider=llm),
    "factuality": ragas_faithfulness,                    # 替换旧版
    "answer_correctness": ragas_correctness,             # 新增
    "relevance": RagasAnswerRelevancy(llm_provider=llm), # 替换旧版
}
```

---

## 与旧版的对比

| 对比项 | 旧版 FactualityChecker (v1.0) | 新版 RagasFaithfulness (v2.0) |
|--------|------------------------------|-------------------------------|
| 评估方式 | LLM Judge 交叉评判 | 声明分解 + F1 + 语义相似度 |
| 分数范围 | 0.0 或 1.0（二元） | 0.0 ~ 1.0（平滑） |
| 需要配置 | quality_judge_model | 无需额外配置（复用已有 embedding） |
| 依赖 | prompts/quality/factuality_judge.yaml | 无模板依赖 |
| 可读性 | LLM 返回的 JSON | 中文描述 \|\| 技术数据 |
| 异常处理 | fail-open → score=0.0 | 回退到语义相似度 |

---

## 测试

```bash
# 导入验证
python -c "from src.quality.ragas_checker import RagasFaithfulness, RagasFactualCorrectness; print('OK')"
```

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 FactualityChecker（LLM 交叉评判） |
| 2026-06-06 | **v2.0 RAGAS 重构**。替换为 RagasFaithfulness，新增 RagasFactualCorrectness 维度 |
