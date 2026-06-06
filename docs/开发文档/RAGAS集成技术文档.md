# RAGAS 集成技术文档

> 本文档详细说明 AI Assistant 系统中 RAGAS 风格质量评估的
> 技术原理、算法实现和源码结构。

---

## 一、概述

### 1.1 为什么引入 RAGAS

原有的质量评估系统存在以下问题：

| 问题 | 表现 | 原因 |
|------|------|------|
| 分数非黑即白 | 要么 100 分要么 0 分 | 基于 LLM 的二元判定 |
| 无法检测噪声 | 引用噪声文档仍得高分 | 只检查"回答 vs 上下文"，不检查"回答 vs 事实" |
| 无答案正确性 | 无法验证回答是否事实正确 | 缺少与标准答案的对比维度 |

### 1.2 什么是 RAGAS

RAGAS（Retrieval-Augmented Generation Assessment）是一个开源的 RAG 评估框架。
其核心思路是：将回答拆解为原子事实声明（claims），然后逐条验证。

本项目实现了 RAGAS 的核心算法，但不依赖 ragas 包，使用项目已有的
DeepSeek LLM 和 Embedding 模型从零实现。

---

## 二、评分算法原理

### 2.1 核心公式

```text
最终得分 = F1 × 权重 + 语义相似度 × (1 - 权重)
```

默认权重 = 0.5，即事实准确性和语义接近程度各占一半。

### 2.2 三个计算步骤

#### 步骤一：声明分解（Claim Decomposition）

将文本拆解为原子事实：

```text
输入文本："系统使用PostgreSQL 15，Redis 6用于缓存"
分解结果：
  claim 1: "数据库使用PostgreSQL 15"
  claim 2: "Redis 6用于缓存"
```

**实现方式**：优先使用 LLM 分解，失败时回退到按句子分割。
**源码位置**：`_decompose_claims()` 函数，ragas_checker.py:24-57

#### 步骤二：F1 分数计算

对比回答和标准答案的声明集合：

```text
回答声明: ["内存4GB", "磁盘20GB"]
标准声明: ["内存8GB", "磁盘50GB"]

TP(两者都有) = 0
FP(回答有但标准没有) = "内存4GB", "磁盘20GB" = 2
FN(标准有但回答没有) = "内存8GB", "磁盘50GB" = 2

F1 = 2 x 0 / (2 x 0 + 2 + 2) = 0.0
```

**关键词匹配**：使用 jieba 分词，要求 30% 以上的有意义分词重叠。
**源码位置**：`_check_support()` 和 `_calculate_f1()`，ragas_checker.py:60-108

#### 步骤三：语义相似度

使用项目的 Embedding 模型计算余弦相似度：

```text
回答: "内存4GB"
标准: "内存8GB"
-> 向量余弦相似度 = 0.85（语义接近，仅数值不同）
```

**源码位置**：`_semantic_similarity()`，ragas_checker.py:111-125

### 2.3 数值一致性检查（新增）

针对"30 vs 20"这类数值不同但语义接近的问题，增加数值提取和逐对比较：

```text
回答: "top_k=30"   -> 提取数字 [30]
标准: "top_k=20"   -> 提取数字 [20]
比对: 30 != 20 -> 数值一致性 = 0.0
调整后 F1 = 原F1 x 0.0 = 0.0
```

**源码位置**：`_check_numeric_consistency()`，ragas_checker.py:133-157

---

## 三、五个评估维度

### 3.1 安全性（Safety）

| 项目 | 说明 |
|------|------|
| 检查器 | SafetyChecker（未改动） |
| 原理 | 关键词预筛 + LLM 语义评判 |
| 分数类型 | 0 或 1（安全相关不适合平滑评分） |
| 源码 | `src/quality/safety.py` |

### 3.2 事实性（Factuality）

| 项目 | 说明 |
|------|------|
| 原方案 | FactualityChecker（LLM 交叉验证） |
| 现方案 | **RagasFaithfulness** |
| 原理 | 将回答拆为 claims，检查每个 claim 是否能在检索到的上下文中找到支持 |
| 分数逻辑 | 被支持的 claims / 总 claims |
| 需要参数 | answer + context |
| 源码 | `RagasFaithfulness`，ragas_checker.py:240-297 |

### 3.3 答案正确性（Answer Correctness）— 新增

| 项目 | 说明 |
|------|------|
| 原方案 | 不存在 |
| 现方案 | **RagasFactualCorrectness**（新增维度） |
| 原理 | F1(声明重叠) x 0.5 + 语义相似度 x 0.5 + 数值一致性惩罚 |
| 分数逻辑 | 平滑 0~1，需传入标准答案（ground_truth） |
| 需要参数 | answer + ground_truth |
| 无标准答案时 | 返回 0.5，跳过校验 |
| 源码 | `RagasFactualCorrectness`，ragas_checker.py:164-233 |

### 3.4 相关性（Relevance）

| 项目 | 说明 |
|------|------|
| 原方案 | RelevanceChecker（LLM 评判） |
| 现方案 | **RagasAnswerRelevancy** |
| 原理 | 语义相似度 x 0.5 + 问题术语覆盖率 x 0.3 + 回答长度因子 x 0.2 |
| 分数逻辑 | 平滑 0~1 |
| 需要参数 | query + answer |
| 源码 | `RagasAnswerRelevancy`，ragas_checker.py:304-373 |

### 3.5 检索质量（Retrieval Quality）

| 项目 | 说明 |
|------|------|
| 检查器 | RetrievalQualityChecker（未改动） |
| 原理 | 基于检索分数的纯数值计算 |
| 分数类型 | 连续值 |
| 源码 | `src/quality/retrieval_quality.py` |

---

## 四、系统架构

### 4.1 数据流

```text
用户提问 -> QueryEngine.query() / query_eval()
              |
         ------+-------
         | 检索文档   |
         ------+-------
               |
         ------+-------
         | LLM 生成   |
         ------+-------
               |
         ------+---------------
         | QualityGuard       |
         |  +---------------+ |
         |  | Safety        | |
         |  | Factuality    | |
         |  | Correctness   | |  <- 新增
         |  | Relevance     | |
         |  | Retri. Quality| |
         |  +---------------+ |
         ------+---------------
               |
         ------+---------
         | 干预引擎      |
         ------+---------
               |
         ------+---------
         | 前端展示      |
         +---------------+
```

### 4.2 关键文件

| 文件 | 作用 |
|------|------|
| `src/quality/ragas_checker.py` | RAGAS 三个检查器的实现（新增） |
| `src/quality/guard.py` | QualityGuard 编排器，管理所有检查器的执行 |
| `src/quality/intervention.py` | 干预引擎，根据检查结果决定是否拦截/降级 |
| `src/quality/base.py` | QualityJudge 抽象基类 |
| `src/knowledge/query_engine.py` | 查询引擎，初始化时挂载 QualityGuard |
| `src/api/routes/query.py` | API 路由，将请求参数转发给 QualityGuard |
| `src/api/schemas.py` | 数据模型，含 ground_truth 字段 |

### 4.3 配置项

guard.py 中新增的配置：

```python
_LLM_DIMENSIONS = ["safety", "factuality", "answer_correctness", "relevance"]

_DIMENSION_LABELS = {
    "safety": "安全",
    "factuality": "事实性",
    "answer_correctness": "答案正确性",
    "relevance": "相关性",
    "retrieval_quality": "检索质量",
}
```

intervention.py 中新增的映射：

```python
_DIMENSION_TO_PREFIX = {
    "safety": "safety",
    "factuality": "factuality",
    "answer_correctness": "factuality",
    "retrieval_quality": "retrieval",
    "relevance": "relevance",
}

_DIMENSION_PRIORITY = {
    "safety": 1,
    "factuality": 2,
    "answer_correctness": 2,
    "retrieval_quality": 3,
    "relevance": 4,
}
```

---

## 五、前端改动

| 改动 | 说明 |
|------|------|
| 新增 answer_correctness 标签 | dimLabels 映射中添加中文名 |
| 去掉 Pass/Fail 徽章 | 避免低分但显示 Pass 的混淆 |
| 两行详情展示 | 通俗描述 + 技术数据用虚线分隔 |
| 标准答案输入框 | 在输入栏上方新增，选填 |
| 更新五维度提示 | 从"四个维度"改为"五个维度" |

---

## 六、测试文档

10 组测试问题及标准答案见：`docs/测试文档/前端测试问题.md`
