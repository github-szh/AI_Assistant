# RAG 回答相关性评估（Relevance Evaluation）— RAGAS 版

> RAGAS 风格的相关性检查器，取代原有的 LLM 评判方案。
> 基于语义相似度 + 问题术语覆盖率实现平滑评分。
>
> 实现位置：`src/quality/ragas_checker.py`（class RagasAnswerRelevancy）
> 版本：v2.0（RAGAS）

---

## 概述

RagasAnswerRelevancy 通过三个方面衡量回答与问题的相关程度：

| 维度 | 权重 | 说明 |
|------|:----:|------|
| 语义相似度 | 50% | 问题与回答的 embedding 余弦相似度 |
| 术语覆盖率 | 30% | 问题中的关键术语是否在回答中出现 |
| 回答长度因子 | 20% | 回答是否有足够的内容（过短的回答扣分） |

最终得分 = 语义相似度 x 0.5 + 术语覆盖率 x 0.3 + 长度因子 x 0.2

### 与旧版的对比

| 对比项 | 旧版 RelevanceChecker (v1.0) | 新版 RagasAnswerRelevancy (v2.0) |
|--------|----------------------------|----------------------------------|
| 评估方式 | LLM Judge 三维度审查 | embedding 语义相似度 + jieba 术语覆盖 |
| 分数范围 | 0.0 ~ 1.0（连续，但来自 LLM） | 0.0 ~ 1.0（平滑，纯计算） |
| 依赖服务 | 需要 LLM API | 无需额外调用（复用已有 embedding） |
| 响应速度 | 慢（需 LLM 推理） | 快（纯向量计算 + 分词） |
| Prompt 模板 | prompts/quality/relevance_judge.yaml | 无模板依赖 |

---

## 算法原理

### 1. 语义相似度

使用项目的 Embedding 模型计算问题与回答的余弦相似度：

```text
问题: "部署需要多少内存和磁盘？"
回答: "最低配置4GB内存，20GB磁盘"
→ 两者都在说"内存""磁盘"→ 相似度 ≈ 0.90
```

### 2. 术语覆盖率

用 jieba 对问题分词，过滤停用词后，统计有多少关键词出现在回答中：

```text
问题分词: ["部署", "需要", "多少", "内存", "磁盘"]
去停用词: ["部署", "内存", "磁盘"]
回答命中: "内存"✓ "磁盘"✓ "部署"✓
覆盖率 = 3/3 = 100%
```

### 3. 长度因子

过短的回答（如"是"、"不知道"）给予惩罚：

```text
回答长度 20+ 字符 → 满分 1.0
回答长度 10 字符 → 长度因子 = 10/20 = 0.5
```

### 示例计算

```text
问题: 部署需要多少内存？
回答: 4GB

语义相似度 = 0.85
术语覆盖率 = 100%（"部署""内存"都在回答中）
长度因子   = 3/20 = 0.15

最终分 = 0.85 x 0.5 + 1.0 x 0.3 + 0.15 x 0.2 = 0.755
→ "高度相关"（>= 0.7）
```

---

## 使用方式

### 代码调用

```python
from src.quality.ragas_checker import RagasAnswerRelevancy

checker = RagasAnswerRelevancy(llm_provider=llm)
verdict = checker.evaluate(
    query="什么是 RAG 技术？",
    answer="RAG 是检索增强生成技术。",
)
print(f'分数: {verdict.score}')
print(f'详情: {verdict.details}')
```

### 集成到 QualityGuard

在 `src/knowledge/query_engine.py` 中已配置：

```python
from src.quality.ragas_checker import RagasAnswerRelevancy
checkers = {
    ...
    "relevance": RagasAnswerRelevancy(llm_provider=llm),
}
```

---

## 分数解读

| 分数范围 | 可读描述 | 说明 |
|:-------:|---------|------|
| >= 0.7 | 高度相关 | 回答直接回应了问题核心 |
| 0.4 ~ 0.7 | 部分相关 | 回答涉及了问题但不够全面 |
| < 0.4 | 相关性较低 | 回答偏离问题或内容不足 |

---

## 配置

无需额外配置。RagasAnswerRelevancy 不依赖 LLM 调用和 Prompt 模板，
只使用项目已有的 Embedding 模型和 jieba 分词。

---

## 测试

```bash
python -c "from src.quality.ragas_checker import RagasAnswerRelevancy; print('OK')"
```

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 RelevanceChecker（LLM 三维度审查） |
| 2026-06-06 | v2.0 RAGAS 重构。替换为 RagasAnswerRelevancy（语义 + 术语覆盖） |
