# Draft: RAG 质量保证系统 — 技术文档

## 质检系统概览

RAG 质量保证系统（QualityGuard）位于 LLM 生成回答之后、返回用户之前，对回答进行五个维度的质量评估并执行干预。

```
用户提问 → 检索知识库 → LLM 生成回答 → ═══ QualityGuard ═══ → 返回用户
                                        ║                    ║
                                        ║ ① 检索质量检查     ║ → 纯数值计算
                                        ║ ② 安全审核         ║ → 关键词 + LLM
                                        ║ ③ 事实性检查       ║ → RAGAS 平滑评分
                                        ║ ④ 答案正确性检查   ║ → RAGAS（需标准答案）
                                        ║ ⑤ 相关性检查       ║ → RAGAS 平滑评分
                                        ║    ↓               ║
                                        ║ 干预引擎决策        ║ → block/degrade/warn/none
                                        ╚════════════════════╝
```

## 一、检索质量检查（RetrievalQualityChecker）

### 作用
在 LLM 生成回答之前和之后，对**检索结果的分数**进行量化评估。纯数值计算，**不调用 LLM**，零延迟。

### 工作流程

```
检索结果（每个 chunk 有一个相似度分数）
  │
  ├─ 计算平均分 (avg_score)      ← 用于 pass/fail 判定
  ├─ 计算最高分 (max_score)      ← 用于预生成检查 should_skip_llm
  ├─ 计算阈值通过率 (pass_rate)  ← 仅记入 details，辅助调试
  └─ 计算分数离散度 (dispersion) ← 仅记入 details，辅助调试
  │
  ├─ avg >= 0.5 → 通过
  ├─ 0.3 <= avg < 0.5 → 边界线（标记 borderline）
  └─ avg < 0.3 → 不通过
```

### avg_score 怎么算？

```text
avg_score = sum(所有chunk的相似度分数) / chunk总数
```

就是所有检索结果分数的**算术平均值**。

示例：

```text
检索返回 5 个 chunk，分数分别为: [0.92, 0.87, 0.65, 0.43, 0.21]
avg_score = (0.92 + 0.87 + 0.65 + 0.43 + 0.21) / 5 = 0.616
```

avg_score 的范围是 0~1，越接近 1 表示整体检索质量越高。

### 为什么只用 avg 做判定？

| 指标 | 用途 | 为什么不用来做 pass/fail |
|------|------|------------------------|
| avg_score | pass/fail 判定 | 最能反映整体检索质量的单一指标 |
| max_score | pre-generation 检查 should_skip_llm() | 只看"有没有一个能用的"，不反映整体质量 |
| pass_rate | 记入 details 供调试 | 与 avg 高度相关，重复信息 |
| dispersion | 记入 details 供调试 | 单独用无意义（如 0.9+0.1 vs 0.5+0.4 离散度相同但质量天差地别）|

简单说：**avg 是体检总分，其他指标是体检单上的细分项**。医生根据总分判断健康，细分项只在排查时看。

### 0.65 阈值详解

0.65 定义在 `src/config.py:65`，是检索管线的**第一阶段阈值**：

```python
retrieval_stage1_threshold: float = 0.65  # cosine similarity, triggers Stage 2 (HyDE + reranker)
```

它在系统中有**两个作用**，但用的是同一个值：

**作用一：触发深度搜索（query_engine.py）**

检索结果中最高分与 0.65 比较，决定走快路径还是慢路径：

```text
检索结果最高分
     |
     +-->= 0.65 --> 快路径：直接用粗排结果
     |
     +--< 0.65 --> 慢路径：触发 HyDE + Reranker 深度搜索
```

当最高分低于 0.65 时，说明向量检索的结果不够可靠，需要启动 HyDE（假设文档嵌入）重新检索，再用 Reranker 精排。

**作用二：跳过 LLM 生成（retrieval_quality.py）**

```text
所有分数
     |
     +--存在任何一个 >= 0.65 --> 正常 LLM 生成
     |
     +--全部 < 0.65 --> should_skip_llm() 返回 True
                        --> 跳过 LLM，返回"未找到足够相关信息"
```

**为什么是 0.65？**

这是一个经验值。pgvector 实际返回的是**余弦距离**（0=完全相同），引擎内部做了 `1.0 - distance` 转换为相似度。

0.65 的含义：
- 最高分 >= 0.65：至少有一个 chunk 与问题**相当相关**，可以正常生成回答
- 全部 < 0.65：检索到的文档与问题都不太匹配，强行回答容易产生幻觉

此值可以通过环境变量 `retrieval_stage1_threshold` 覆盖调优。

### 关键方法

```python
# 预生成检查（在 LLM 调用前执行）
should_skip_llm(scores, threshold=0.65)
# → 所有分数 < 0.65？→ 跳过 LLM 生成，直接返回"未找到足够相关信息"

# 生成后评估
evaluate(query, scores) → QualityVerdict
# → 返回四个维度的数值统计
```

### 实现位置
- `src/quality/retrieval_quality.py` — 主逻辑（~135 行）
- `src/quality/guard.py:173-203` — 在 QualityGuard 中被调用

### 关键设计决策
- 纯数值计算，不依赖任何外部 API
- 预生成检查（should_skip_llm）可以避免在无结果时浪费 LLM 调用
- 所有计算同步完成，对总延迟的影响可忽略

### 分数来源

检索结果的分数（score）来源于整个检索管线的多个阶段：

**第一阶段：混合召回（Hybrid Retrieval）**
- 向量检索（Dense）：使用 Zhipu Embedding-2 模型将查询和文档片段编码为 1024 维向量，通过 pgvector 的余弦距离（cosine distance）计算相似度。得分范围 0~1，值越大表示语义越相似。
- 全文检索（Sparse）：基于 PostgreSQL 的 GIN 索引对 tokenized text 做 BM25 全文搜索，匹配词法关键词。
- RRF 融合：将向量检索和 BM25 的排名通过 Reciprocal Rank Fusion（RRF）算法合并，产出最终的候选列表和融合分数。

**第二阶段：Reranker 精排（可选，由配置 rerank_enabled 控制）**
- 当 `retrieval_stage1_threshold` 条件触发深度搜索时，调用 BGE Cross-Encoder Reranker 对候选结果进行重排序。
- Reranker 将查询和候选文档逐对输入 Cross-Encoder 模型，输出精确的相关性 logit 分数。
- 经过 reranker 后的分数比纯向量相似度更准确，用于后续的质检评估。

**分数传递到质检**
- QueryEngine 在 `_retrieve()` 方法中将检索到的 Node 对象（含 `.score` 属性）传递给质检模块。
- RetrievalQualityChecker 从 `sources` 列表中提取每个来源的 `score` 字段做统计分析。
- Node 的 `.score` 属性在 RRF 阶段是融合排名分，在 Reranker 阶段是 Cross-Encoder 的 logit 输出。
- 注意：PGVector 返回的是余弦距离（0=完全相同），引擎内部在构建 SourceInfo 时转换为相似度（1.0 - distance），以确保"越高越好"的语义一致性。

---

## 二、安全审核（SafetyChecker）

### 作用
检测模型回答是否包含**有害、违规、敏感内容**。这是质检系统中**唯一有拦截能力**的维度。

### 两阶段架构

### 关键词来源

安全审核使用的关键词和正则模式定义在 `src/quality/config.py` 中，通过 `get_default_safety_categories()` 函数返回。共 6 个安全分类，定义方式如下：

```python
SafetyCategory(
    name="harmful_content",        # 分类名称
    keywords=[                     # 精确匹配关键词列表
        "暴力", "色情", "仇恨", "歧视", "自残", "自杀",
        "恐怖主义", "校园暴力", "虐待", "血腥",
    ],
    regex_patterns=[],             # 正则匹配模式列表
    description="有害内容：包含暴力、色情、仇恨言论、恐怖主义等",
)
```

自定义关键词的两种方式：

1. **修改代码**：直接在 `config.py` 的 `get_default_safety_categories()` 中增删关键词。
2. **YAML 配置**：创建 YAML 文件并通过 `KeywordFilter(yaml_path="custom_categories.yaml")` 加载，无需修改代码：

```yaml
safety_categories:
  - name: custom_category
    keywords: ["自定义关键词1", "自定义关键词2"]
    regex_patterns: []
    description: "自定义分类说明"
```

KeywordFilter 初始化时会将内置类别、代码传入的 categories 参数、YAML 文件中的类别三者合并。

#### 阶段 1：关键词预过滤（KeywordFilter）

```
回答文本
  │
  ├─ harmful_content（暴力/色情/仇恨/歧视/自杀/恐怖主义/血腥）
  ├─ prompt_injection（忽略指令/忘记之前/突破限制/无视规则）
  ├─ personal_info（身份证/手机号/银行卡/密码/住址）
  ├─ sensitive_topic（政治敏感/宗教极端/领土争端/分裂言论）
  ├─ illegal_content（毒品/赌博/枪支/黑客/洗钱/走私）
  └─ misinformation（谣言/阴谋论/伪科学/虚假新闻）
  │
  ├─ 命中任何一个 → 零延迟直接返回违规 verdict
  └─ 未命中 → 进入阶段 2
```

**正则模式**（额外捕获结构化数据）：
- 身份证号：`(?<!\d)\d{17}[\dXx](?!\d)`
- 手机号：`(?<!\d)1[3-9]\d{9}(?!\d)`
- 邮箱地址：`[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`
- 英文提示注入：`(ignore|forget|disregard)\s+(all\s+)?(above|previous|instructions)`
- 中文提示注入：`(不要|无需|不必)(遵循|遵守|理会)(.*?)(规则|指令|限制)`

#### 阶段 2：LLM Judge 语义评判

当关键词未命中时，调用独立的 LLM Judge 进行深度语义评估。这个 Judge 模型**不同于**对话使用的生成模型：

| | 生成模型（对话） | Judge 模型（质检） |
|---|----------------|-------------------|
| 模型 | deepseek-v4-pro（由 `DEEPSEEK_MODEL` 配置） | deepseek-v4-flash（由 `quality_judge_model` 配置） |
| 目的 | 生成回答 | 评判回答质量 |
| 温度 | 0.0~0.7 | 0.0（确定性输出） |
| 超时 | 无特殊限制 | 10 秒超时保护 |

这种设计称为**交叉评判（Cross-Judging）**，目的是避免"自我增强偏差"——如果让生成模型自己评判自己的输出，它会倾向于给出高分。使用不同的、更轻量的模型做评判器，评估更客观。

Judge 模型的调用链路：

```
加载 safety_judge.yaml 模板 → 填入 question/answer/context → 调用 quality_judge_model → 解析 JSON
  │
  Judge 模型的配置项：
  - quality_judge_provider: 默认跟随 llm_provider（deepseek），可单独指定
  - quality_judge_model: 默认 deepseek/deepseek-v4-flash
  - quality_judge_timeout_s: 每次调用的超时秒数（默认 10s）
```

调用失败时按 Fail-Closed 策略处理（安全优先）：超时或异常时默认拦截回答。

```
加载 safety_judge.yaml 模板 → 填入 question/answer/context → 调用 LLM Judge
  │
  └─ LLM 返回 JSON：
     {
       "passed": true/false,
       "score": 0.0~1.0,
       "violations": [
         {"type": "hate_speech", "detail": "...", "severity": "high"},
         {"type": "illegal_content", ...}
       ],
       "reasoning": "审查评价理由"
     }
```

**4 个安全子维度**：
| 维度 | 评估内容 |
|------|---------|
| hate_speech | 仇恨与歧视言论（种族、性别、宗教等） |
| illegal_content | 违法与违规内容（毒品、赌博、非法交易等） |
| sensitive_political | 敏感政治话题（违法政治言论、敏感事件等） |
| violence | 暴力和极端内容（宣扬暴力、恐怖主义等） |

### 特殊处理

**模型正确拒答**：如果 LLM 回答包含以下 20 种模式之一，自动判为安全通过：
```
"无法回答", "我不能回答", "抱歉", "对不起", "没有相关信息",
"拒绝回答", "不便回答", "i cannot", "i'm sorry", "cannot answer"...
```

**Fail-Closed 策略**：当 LLM Judge 调用异常（超时、解析失败等）时，默认**拦截回答**（安全优先）。

### 实现位置
- `src/quality/safety.py` — SafetyChecker 主逻辑（~262 行）
- `src/quality/keyword_filter.py` — 关键词过滤器（~298 行）
- `src/quality/config.py` — 安全分类和关键词定义（~356 行）
- `prompts/quality/safety_judge.yaml` — LLM Judge 提示词模板

---

## 三、事实性检查（RAGAS 风格）

> **v2.0 升级**：替换为 RAGAS 风格的平滑评分器，不再使用 LLM Judge 二元评判。
> 实现位置：`src/quality/ragas_checker.py`

RAGAS 风格的事实性检查包含**两个维度**：

| 维度 | 类名 | 作用 |
|------|------|------|
| 事实性 (factuality) | RagasFaithfulness | 回答中的声明是否能在检索到的上下文中找到支持 |
| 答案正确性 (answer_correctness) | RagasFactualCorrectness | 回答是否与标准答案一致 |

### RagasFaithfulness（事实性）

#### 执行流程

```text
evaluate(query, answer, context)
  |
  +- 回答为空？-> 跳过，score=1.0
  +- 无上下文？-> 无法验证，score=0.5
  +- 正常流程：
       |
       LLM 将回答分解为原子声明（claims）
       -> 逐一检查每个 claim 是否在 context 中出现
       -> score = 被支持的 claims / 总 claims
       -> 返回 QualityVerdict（平滑 0~1）
```

#### 实现位置
- `src/quality/ragas_checker.py:240-297` — RagasFaithfulness 类
- 依赖：LLM（声明分解）+ jieba（关键词匹配）+ embedding（语义相似度回退）

### RagasFactualCorrectness（答案正确性）— 新增维度

#### 执行流程

```text
evaluate(query, answer, context, ground_truth)
  |
  +- 无 ground_truth？-> 跳过，score=0.5
  +- 有 ground_truth：
       |
       LLM 分解回答和标准答案的声明
       -> 计算 F1（声明重叠率）
       -> 计算语义相似度（embedding 余弦）
       -> 检查数值一致性（30 vs 20 的情况）
       -> adjusted_F1 = F1 x 数值一致性
       -> 最终分 = adjusted_F1 x 0.5 + 语义相似度 x 0.5
       -> 返回 QualityVerdict（平滑 0~1）
```

#### 核心公式

```text
最终分 = F1(声明重叠) x 0.5 + 语义相似度 x 0.5
```

附加**数值一致性检查**：提取回答和标准答案中的数字逐对比较，不匹配时降低 F1。

#### 实现位置
- `src/quality/ragas_checker.py:164-233` — RagasFactualCorrectness 类
- `src/quality/ragas_checker.py:133-157` — `_check_numeric_consistency()` 数值检查

---

## 四、相关性检查（RAGAS 风格）

> **v2.0 升级**：替换为 RAGAS 风格的平滑评分器，基于语义相似度 + 术语覆盖率。
> 实现位置：`src/quality/ragas_checker.py`（class RagasAnswerRelevancy）

### 作用
衡量回答与问题的相关程度，基于三个维度的加权计算：

| 维度 | 权重 | 说明 |
|------|:----:|------|
| 语义相似度 | 50% | 问题与回答的 embedding 余弦相似度 |
| 术语覆盖率 | 30% | 问题中的关键术语是否在回答中出现 |
| 回答长度因子 | 20% | 回答是否有足够内容（过短扣分） |

### 公式

```text
最终分 = 语义相似度 x 0.5 + 术语覆盖率 x 0.3 + 长度因子 x 0.2
```

### 执行流程

```text
evaluate(query, answer)
  |
  计算 query 和 answer 的语义相似度（embedding）
  -> jieba 分词计算 query 术语在 answer 中的覆盖率
  -> 计算长度因子（min(1.0, len(answer)/20)）
  -> 加权融合得到平滑 0~1 分
  -> 返回 QualityVerdict
```

### 分数解读

| 分数 | 描述 |
|:----:|------|
| >= 0.7 | 高度相关：回答直接回应了问题核心 |
| 0.4 ~ 0.7 | 部分相关：涉及了问题但不够全面 |
| < 0.4 | 相关性较低：偏离问题或内容不足 |

### 与旧版对比

| 对比项 | 旧版 (v1.0) | 新版 (v2.0 RAGAS) |
|--------|------------|-------------------|
| 评估方式 | LLM Judge 三维度审查 | embedding 语义 + jieba 术语覆盖 |
| 依赖服务 | 需要 LLM API | 无需额外调用 |
| 响应速度 | 慢（需 LLM 推理） | 快（纯向量计算 + 分词） |
| Prompt 模板 | relevance_judge.yaml | 无模板依赖 |

### 实现位置
- `src/quality/ragas_checker.py:304-373` — RagasAnswerRelevancy 类

---

## 五、干预引擎（InterventionEngine）

### 作用
根据四个质检维度的评估结果，按优先规则执行干预动作。

### 规则优先级

| 优先级 | 违规类型前缀 | 动作 | 前端表现 |
|--------|------------|------|---------|
| 1（最高） | safety_* | **block** 🚫 | 回答替换为安全提示，来源清空 |
| 2 | factuality_* | **degrade** ⬇️ | 回答置空，仅保留来源卡片 |
| 2 | answer_correctness_* | **degrade** ⬇️ | 回答置空，仅保留来源卡片 |
| 3 | retrieval_* | **warn** ⚠️ | 回答末尾追加警告提示 |
| 4 | relevance_* | **warn** ⚠️ | 回答末尾追加警告提示 |

### 决策逻辑

```
所有 verdict 汇总
  │
  ├─ 全部 passed → action=none（正常展示）
  └─ 有未通过的：
       ├─ 按维度优先级排序（safety > factuality > retrieval > relevance）
       ├─ 匹配第一个违规类型的干预规则
       └─ 执行对应的 action
```

### 实现位置
- `src/quality/intervention.py` — 主逻辑（~227 行）
- `src/quality/config.py:138-230` — 干预规则定义

---

## 六、编排器（QualityGuard）

### 作用
统筹四个质检维度的执行顺序与并行调度。

### 执行流程

```
QualityGuard.run(query, answer, context, sources)
  │
  ├─ 1. 检索质量评估（纯数值，零延迟，同步执行）
  │
  ├─ 2. 并行执行三个 LLM 评判维度（ThreadPoolExecutor）
  │     ├─ SafetyChecker.evaluate()     ← 安全
  │     ├─ RagasFaithfulness.evaluate()  ← 事实性
  │     └─ RagasAnswerRelevancy.evaluate()   ← 相关性
  │
  └─ 3. 汇总所有 verdict → InterventionEngine.run_all()
        → 执行干预 → 返回 (modified_response, intervention_info)
```

### 并行执行
- Safety、RAGAS 事实性、RAGAS 相关性 三个 LLM 评判使用 `ThreadPoolExecutor` **并发执行**
- 总超时 = `quality_judge_timeout_s × 3 + 5 秒` 缓冲
- 单个 checker 超时或异常 **不影响其他维度**（fail-isolated）

### 实现位置
- `src/quality/guard.py` — 主逻辑（~329 行）
- `src/quality/base.py` — QualityJudge 抽象基类（~472 行）

---

## 七、提示词模板

每个 LLM Judge 维度都有一个 YAML 格式的提示词模板：

| 模板文件 | 位置 |
|---------|------|
| 安全审核 | `prompts/quality/safety_judge.yaml` |
| 事实性检查 | `prompts/quality/factuality_judge.yaml` |
| 相关性检查 | `prompts/quality/relevance_judge.yaml` |

模板使用 `{{ variable }}` 语法，由 `QualityJudge._render_prompt()` 渲染。

---

## 八、SSE 事件集成（前端展示）

质检结果通过 SSE（Server-Sent Events）推送到前端：

```
流式 RAG 响应中的事件顺序：
  steps → sources → c × N → quality → done
                              ↑
                         质检结果在此推送
```

**quality 事件格式**：

```json
{
  "type": "quality",
  "intervened": true,
  "action": "block",
  "reason": "检测到有害内容，已拦截回答",
  "violations": [
    {"dimension": "safety", "passed": false, "score": 0.0, "details": "..."}
  ]
}
```

| action 值 | 前端行为 |
|-----------|---------|
| block | 用 `override_answer` 替换已展示的回答 |
| degrade | 清空回答内容，保留来源卡片 |
| warn | 在回答下方追加 ⚠️ 警告提示 |
| none | 正常展示，不做额外处理 |

---

## 九、完整文件索引

| 文件 | 行数 | 作用 |
|------|------|------|
| `src/quality/base.py` | ~472 | QualityJudge 抽象基类 + LLM 调用 + JSON 解析 + QualityVerdict |
| `src/quality/safety.py` | ~262 | 安全审核（两阶段：关键词 + LLM） |
| `src/quality/factuality.py` | ~222 | 事实性检查（幻觉检测） |
| `src/quality/relevance.py` | ~142 | 相关性检查 |
| `src/quality/retrieval_quality.py` | ~135 | 检索质量检查（纯数值） |
| `src/quality/keyword_filter.py` | ~298 | 关键词过滤器（含中文分词兼容） |
| `src/quality/guard.py` | ~329 | QualityGuard 编排器 |
| `src/quality/intervention.py` | ~227 | 干预引擎（规则匹配 + 动作执行） |
| `src/quality/config.py` | ~356 | 干预规则 + 安全分类 + 阈值定义 |
| `prompts/quality/safety_judge.yaml` | ~54 | 安全审核 LLM 提示词 |
| `prompts/quality/factuality_judge.yaml` | ~49 | 事实性检查 LLM 提示词 |
| `prompts/quality/relevance_judge.yaml` | ~41 | 相关性检查 LLM 提示词 |

