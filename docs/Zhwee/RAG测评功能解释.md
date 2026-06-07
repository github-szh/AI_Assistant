# RAG测评 功能解释

> 日期：2026-06-07

## 目录

- [一、整体流程（详细版）](#一整体流程详细版)
  - [阶段 0：前端触发](#阶段-0前端触发)
  - [阶段 1：后端路由处理](#阶段-1后端路由处理)
  - [阶段 2：检索管线](#阶段-2检索管线-query_eval--_retrieve)
    - [2.1 问题改写](#21-问题改写可选)
    - [2.2 Level 1：文档摘要预筛选](#22-level-1文档摘要预筛选)
    - [2.3 Level 2：混合召回](#23-level-2混合召回-vector--bm25--rrf)
    - [2.4 Stage 1 阈值判断](#24-stage-1-阈值判断)
    - [2.5 HyDE 深度搜索](#25-hyde-深度搜索条件触发)
    - [2.6 上下文构造与置信度](#26-上下文构造与置信度)
  - [阶段 3：LLM 生成](#阶段-3llm-生成)
  - [阶段 4：QualityGuard 多维度质检](#阶段-4qualityguard-多维度质检)
    - [4.1 检索质量评估](#41-检索质量评估纯数值零延迟)
    - [4.2 并行 LLM 评判](#42-并行-llm-评判)
    - [4.3 并行 vs 串行](#43-并行-vs-串行)
  - [阶段 5：干预决策](#阶段-5干预决策-interventionengine)
  - [阶段 6：执行干预](#阶段-6执行干预)
  - [阶段 7：响应组装与前端渲染](#阶段-7响应组装与前端渲染)
  - [完整时序图](#完整时序图)
- [二、五个质检维度详解](#二五个质检维度详解)
  - [1. 检索质量](#1-检索质量-retrieval_quality--纯数值零-llm-调用)
  - [2. 安全性](#2-安全性-safety--llm-judge)
  - [3. 事实性](#3-事实性-factuality--ragas-忠实度)
  - [4. 答案正确性](#4-答案正确性-answer_correctness--ragas-正确性)
  - [5. 相关性](#5-相关性-relevance--纯-embedding--jieba)
- [三、干预引擎](#三干预引擎-interventionengine)
- [四、核心价值](#四核心价值)

---

## 一、整体流程（详细版）

RAG测评的完整链路涉及 7 个阶段，从前端请求到后端返回，共经历约 20+ 步。下面按调用顺序逐一展开。

---

### 阶段 0：前端触发

**文件：** `ai-assistant-web/src/views/EvalView.vue`

用户在 RAG测评页面输入问题（可选填"标准答案"），点击发送：

```typescript
// EvalView.vue 第 235 行
const res = await api.post('/query/eval', {
  question: text,        // 必填：用户问题
  top_k: 3,              // 固定 3：返回 Top-3 来源
  ground_truth: groundTruth.value || undefined,  // 选填：标准答案
})
```

与普通 Chat 的关键区别：
- **非流式** — 等全部结果生成完毕后一次性渲染
- **无 session** — 不保存到 t_session_message
- **可传 ground_truth** — 用于答案正确性校验

---

### 阶段 1：后端路由处理

**文件：** `src/api/routes/query.py` 第 125-174 行

```python
@router.post("/eval", response_model=EvalResponse)
async def query_knowledge_eval(req, engine, user):
    # 1. 检索 + LLM 生成
    eval_result = await engine.query_eval(question, top_k, doc_ids, messages, tenant_id)
    
    # 2. 运行 QualityGuard
    _, intervention = engine.quality_guard.run(query, answer, context, sources, ...)
    
    # 3. 组装响应
    return EvalResponse(answer, sources, quality={维度→VerdictDetail}, intervention)
```

注意：这个路由**自己调用** `quality_guard.run()`，与 `query()` 不同——`query_eval()` 只返回 answer+sources+context，不内置质检。质检在路由层外部执行，这样 RAG测评 可以获得完整的 quality 数据和 intervention 信息。

---

### 阶段 2：检索管线 (query_eval → _retrieve)

**文件：** `src/knowledge/query_engine.py` 第 288-313 行（query_eval）和第 88-237 行（_retrieve）

`query_eval()` 调用 `_retrieve()`，这是一个完整的检索管线，分 5 个子步骤：

#### 2.1 问题改写（可选）

```python
search_question = self._rewrite_question(question, messages)
```

- 如果有多轮对话历史，LLM 将依赖上下文的追问改写为独立问题
- 例如："那模型参数呢？" → "重排序模型的参数量是多少？"
- 单轮对话或 RAG测评（通常无历史消息）→ 跳过，直接使用原始问题
- 改写失败 → 使用原始问题，不阻塞流程

#### 2.2 Level 1：文档摘要预筛选

```python
if doc_count >= settings.two_stage_min_docs:  # 默认 10 篇
    query_emb = emb_mgr.encode_query(search_question)
    relevant = _search_summaries(query_emb, settings.summary_search_top_k, tenant_id)
    target_ids = [r["doc_id"] for r in relevant]
```

**原理：**
1. 将用户问题编码为 Embedding 向量
2. 在 `doc_summaries` 表（存储每篇文档的 AI 摘要 + 向量）中搜索最相关的 Top-5 篇文档
3. 后续检索**只在这些文档的 chunk 中搜索**，大幅缩小范围

**跳过条件：**
- 知识库文档 < 10 篇（`two_stage_min_docs`）
- 调用方已指定 `doc_ids`（直接搜指定文档）
- Embedding 服务不可用（降级为全库搜索）

#### 2.3 Level 2：混合召回 (Vector + BM25 → RRF)

```python
nodes = await self.retriever.retrieve(search_question, ids=target_ids)
```

对 Level 1 筛选出的文档，执行**双路召回 + RRF 融合**：

```
                   用户问题
                   /      \
            Vector 搜索    BM25 搜索
           (pgvector)      (jieba 分词)
                │               │
            dense=6条       sparse=6条
                │               │
                └─── RRF 融合 ──┘
                      │
                  fused=6条
```

**RRF (Reciprocal Rank Fusion)** 公式：
```
RRF_score(chunk) = Σ 1/(k + rank_i(chunk))
```
其中 k=60（平滑常数），`rank_i` 是 chunk 在对应召回列表中的排名。RRF 的优点是不需要分数归一化，对不同量纲的分数也能公平融合。

#### 2.4 Stage 1 阈值判断

```python
top_score = nodes[0].score  # 最高分
if top_score <= settings.retrieval_stage1_threshold:  # 默认 0.65
    → 进入深度搜索（HyDE + Reranker）
else:
    → 快路径，直接取 Top-K
```

- **快路径**：最高分 > 0.65，说明检索质量好，直接取 Top-K 个 chunk 去生成
- **深度搜索**：最高分 ≤ 0.65，说明简单召回不够，需要 HyDE 重写 + Reranker 精排

#### 2.5 HyDE 深度搜索（条件触发）

```python
hyde_text = self._generate_hypothetical(search_question)
nodes = await self.retriever.retrieve_with_rerank(hyde_text, ids=target_ids)
```

**HyDE (Hypothetical Document Embeddings)** 原理：
1. LLM 生成一段"假设答案"（不需要准确，只要看起来像答案）：
   ```
   问题："重排序使用什么模型？"
   假设答案："重排序通常使用基于预训练语言模型的交叉编码器架构..."
   ```
2. 用**假设答案**替代问题去检索——因为假设答案的词汇分布更接近真实文档
3. 检索结果再经过 BGE-reranker-v2-m3 交叉编码器精排

**为什么有效？** 问题和文档存在"词汇鸿沟"——用户用口语提问，文档用专业术语写。HyDE 生成的假设答案更像文档语言，bridge 这个 gap。

#### 2.6 上下文构造与置信度

```python
# 构造上下文字符串
context = "\n\n".join(f"[{i+1}] ({filename})\n{content}" for i, node in enumerate(top_nodes))

# 构造来源列表
sources = [SourceInfo(doc_id, filename, chunk_index, score, snippet) ...]

# 置信度判定
if top_score >= 0.70:   → high
elif top_score >= 0.50: → medium
else:                   → low
```

`query_eval()` 额外的返回：除了 answer 和 sources，还返回 `context`（检索到的原始文档片段），供 QualityGuard 的事实性检查使用。

---

### 阶段 3：LLM 生成

**文件：** `src/knowledge/query_engine.py` 第 307-309 行

```python
prompt = self._build_prompt(question, context)  # 加载 prompts/rag/query.yaml
llm = get_llm()
answer = llm.chat(messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1024)
```

- 使用 `LLM_PROVIDER`（deepseek）的 `DEEPSEEK_MODEL`（deepseek-v4-pro）
- temperature=0.0 确保回答稳定可复现
- Prompt 模板从 `prompts/rag/query.yaml` 加载，包含 role/system 设定和检索上下文注入

---

### 阶段 4：QualityGuard 多维度质检

**文件：** `src/quality/guard.py` 第 106-171 行

```python
def run(self, query, answer, context, sources, **kwargs):
    verdicts = []
    
    # Step 1: 检索质量（纯数值，最先执行）
    self._run_retrieval_quality(query, sources, verdicts)
    
    # Step 2: LLM 评判维度（并行执行）
    self._run_parallel(checkers, query, answer, context, verdicts, ...)
    
    # Step 3: 汇总 → 干预
    modified_response, intervention = self.intervention.run_all(verdicts, original_response)
    return modified_response, intervention
```

#### 4.1 检索质量评估（纯数值，零延迟）

**文件：** `src/quality/retrieval_quality.py`

从 `sources[].score` 提取分数列表，计算 mean/max/dispersion/pass_rate 四个指标，生成 `QualityVerdict(dimension="retrieval_quality", passed, score, details)`。

#### 4.2 并行 LLM 评判

```python
# guard.py 第 235-248 行
with ThreadPoolExecutor(max_workers=4) as executor:
    # 4 个 checker 并发执行
    future_safety = executor.submit(safety_checker.evaluate, query, answer, context)
    future_factuality = executor.submit(factuality_checker.evaluate, query, answer, context)
    future_correctness = executor.submit(correctness_checker.evaluate, query, answer, context, ground_truth=...)
    future_relevance = executor.submit(relevance_checker.evaluate, query, answer, context)
    
    # 等待全部完成，总超时 = timeout_per × n + 5
    done, not_done = wait(futures, timeout=35)
```

每个 checker 内部调用 `_call_judge()` → ThreadPoolExecutor 再次包装 → `_do_llm_call()` 调用 `quality_judge_model`（deepseek-v4-flash）。

**各维度具体算法见第二节。**

#### 4.3 并行 vs 串行

由 `QUALITY_PARALLEL_EVAL` 控制：
- `true`：4 个 checker 并行执行，总延迟 ≈ 最慢单个（~10s）
- `false`：逐个串行，总延迟 ≈ 4 × 单个（~40s）

---

### 阶段 5：干预决策 (InterventionEngine)

**文件：** `src/quality/intervention.py` 第 85-147 行

```python
def evaluate(self, verdicts):
    failed = [v for v in verdicts if not v.passed]
    failed.sort(key=lambda v: DIMENSION_PRIORITY[v.dimension])
    
    for verdict in failed:
        for rule in self.rules:
            if rule.violation_type.startswith(prefix_of(verdict.dimension)):
                return InterventionInfo(intervened=True, action=rule.action, ...)
    
    return InterventionInfo(intervened=False, action="none")
```

**核心逻辑：** 只取优先级最高的违规维度来决定动作：
- 如果 safety 没通过 → block（其他维度即使也没通过，不再匹配）
- 如果 factuality 没通过但 safety 通过 → degrade
- 如果 retrieval_quality 没通过但前两者都通过 → warn
- 全部通过 → none

---

### 阶段 6：执行干预

**文件：** `src/quality/intervention.py` 第 151-205 行

```python
def execute(intervention, original_response):
    if action == "block":
        return {"answer": "抱歉，根据内容安全策略，无法展示此回答。", "sources": []}
    if action == "degrade":
        return {"answer": "", "sources": original_response["sources"]}
    if action == "warn":
        return {"answer": original_answer + "\n\n⚠️ 此回答内容可能存在问题，请谨慎参考。", ...}
    # none: 原样返回
```

注意：RAG测评前端**只用 intervention 信息做展示**（显示红/橙警告框），**不会**真的替换/清空回答——这是和 Chat 质检的关键区别。Chat 质检的 block 会替换 answer，但 RAG测评 是为了诊断，用户需要看到原始回答再判断。

---

### 阶段 7：响应组装与前端渲染

**后端返回的数据结构：**

```python
EvalResponse(
    answer="重排序使用 BGE-reranker-v2-m3 模型，基于 Cross-encoder 架构...",
    sources=[
        SourceInfo(doc_id="abc", filename="系统架构.pdf", score=0.85, snippet="..."),
        ...
    ],
    quality={
        "retrieval_quality":  VerdictDetail(passed=True,  score=0.65, details="平均分=0.65..."),
        "safety":             VerdictDetail(passed=True,  score=0.95, details="内容安全"),
        "factuality":         VerdictDetail(passed=True,  score=0.80, details="4/5 claims supported"),
        "answer_correctness": VerdictDetail(passed=True,  score=0.50, details="无标准答案，跳过"),
        "relevance":          VerdictDetail(passed=True,  score=0.72, details="语义匹配 85%..."),
    },
    intervention=InterventionInfo(intervened=False, action="none", ...)
)
```

**前端渲染（EvalView.vue）：**
1. 回答文本（Markdown 渲染）
2. 来源卡片（文件名 + 片段）
3. 5 张评分卡片（左侧彩色边线 + 分数进度条 + 可读描述）

---

### 完整时序图

```
用户输入问题 + 标准答案(选填)
 │
 ▼
EvalView.vue ──POST /api/query/eval──► query.py
                                          │
                                          ├─ query_eval() ──► _retrieve()
                                          │                      │
                                          │                      ├─ 问题改写 (LLM, 可选)
                                          │                      ├─ Level 1: 摘要预筛选 (Embedding)
                                          │                      ├─ Level 2: Vector+BM25 → RRF
                                          │                      ├─ 阈值判断 (score vs 0.65)
                                          │                      ├─ HyDE 重写 (LLM, 条件触发)
                                          │                      ├─ Reranker 精排 (BGE-cross-encoder)
                                          │                      └─ 构造 context + sources
                                          │                      │
                                          │                      ◄── {nodes, sources, context}
                                          │
                                          ├─ LLM 生成回答 (deepseek-v4-pro)
                                          │
                                          ├─ QualityGuard.run()
                                          │    │
                                          │    ├─ 检索质量 (纯数值, 0ms)
                                          │    └─ 并行执行 4 个 Judge (ThreadPoolExecutor):
                                          │         ├─ 安全 (LLM Judge, deepseek-v4-flash)
                                          │         ├─ 事实性 (LLM 分解 + jieba 支撑检查)
                                          │         ├─ 答案正确性 (需要 ground_truth)
                                          │         └─ 相关性 (Embedding + jieba, 无 LLM)
                                          │
                                          ├─ InterventionEngine.evaluate()
                                          │    └─ 匹配规则 → block/degrade/warn/none
                                          │
                                          └─ 组装 EvalResponse
                                               │
                                          ◄────┘
EvalView.vue ◄── 渲染回答 + 5张评分卡片
```

---

## 二、五个质检维度详解

### 1. 检索质量 (retrieval_quality) — 纯数值，零 LLM 调用

**文件：** `src/quality/retrieval_quality.py`

从检索结果的 `score` 字段提取分数，计算四个指标：

| 指标 | 公式 |
|------|------|
| 平均分 | `mean(scores)` |
| 最高分 | `max(scores)` |
| 阈值通过率 | 分数 > 0.3 的比例 |
| 分数离散度 | `max(scores) - min(scores)` |

判定规则：

| 条件 | passed | 说明 |
|------|--------|------|
| avg ≥ 0.5 | true | 正常通过 |
| 0.1 ≤ avg < 0.5 | true | 边界线（borderline），details 含提示 |
| avg < 0.1 | false | 检索质量过低 |

注意：BGE 重排序后会做 min-max 归一化，最低分恒为 0.0，因此 `_FAIL_THRESHOLD` 设为 0.1。

---

### 2. 安全性 (safety) — LLM Judge

**文件：** `src/quality/safety.py`

只走 LLM 语义评判（关键词预过滤已禁用）：

1. 加载 `prompts/quality/safety_judge.yaml` 模板
2. 将 `question + answer + context` 注入模板
3. 调用 `quality_judge_model`（deepseek-v4-flash）进行语义评判
4. LLM 返回 JSON：`{ passed, score, violations, reasoning }`
5. 如果 LLM 异常 → **fail-closed 策略**：拦截回答（安全优先）

违规范例和对应的干预动作：

| 违规类型 | 动作 |
|----------|------|
| `safety_harmful_content` | block |
| `safety_prompt_injection` | block |
| `safety_personal_info_leak` | block |
| `safety_sensitive_topic` | block |

特殊处理：如果模型正确拒答（如"我无法回答这个问题"），自动判为安全通过。

---

### 3. 事实性 (factuality) — RAGAS 忠实度

**文件：** `src/quality/ragas_checker.py` — `RagasFaithfulness`

检查回答中的**事实声明是否能在检索上下文中找到支撑**，防止幻觉。

**算法步骤：**

1. **声明分解** — 用 LLM 把回答分解为原子级事实声明（claims）：
   ```
   回答: "重排序使用 bge-reranker-v2-m3 模型，基于 Cross-encoder 架构"
   → ["重排序使用 bge-reranker-v2-m3 模型", "基于 Cross-encoder 架构"]
   ```
   如果 LLM 分解失败，回退为按中文句号/换行分割。

2. **支撑检查** — 对每个 claim，用 jieba 分词后检查与检索上下文的**关键词重叠率**（阈值 30%）：
   - 重叠 → 被支撑
   - 不重叠 → 未被支撑

3. **计算分数** — `score = 被支撑的 claims / 总 claims`
   - score ≥ 0.5 → passed=true
   - score < 0.5 → passed=false（标记为幻觉）

**边界情况：**
- 无 answer → passed=true, score=1.0
- 无 context → passed=true, score=0.5（无法验证但不判为幻觉）
- 无法分解 claims → 降级为语义相似度检查

---

### 4. 答案正确性 (answer_correctness) — RAGAS 正确性

**文件：** `src/quality/ragas_checker.py` — `RagasFactualCorrectness`

**需要用户提供标准答案 (ground_truth)**，否则跳过。

**算法步骤：**

1. 将 answer 和 ground_truth 分别用 LLM 分解为 claims
2. 计算 **F1 分数** — 声明级别的精确率和召回率的加权调和平均
3. 计算 **语义相似度** — Embedding 余弦相似度
4. **数值一致性检查** — 提取两段文本中的数字，逐对比较
5. 加权融合：
   ```
   adjusted_f1 = f1 × 数值一致性
   score = adjusted_f1 × 0.5 + 语义相似度 × 0.5
   ```
6. score ≥ 0.5 → passed=true

**边界情况：**
- 无 ground_truth → passed=true, score=0.5（跳过，不参与评测）
- 评测异常 → 降级为纯语义相似度对比

---

### 5. 相关性 (relevance) — 纯 Embedding + jieba

**文件：** `src/quality/ragas_checker.py` — `RagasAnswerRelevancy`

**不调用 LLM**，零 token 成本：

1. **语义相似度** — 问题和回答的 Embedding 余弦相似度（权重 50%）
2. **术语覆盖率** — 问题关键词（jieba 分词 + 去停用词）在回答中的覆盖比例（权重 30%）
3. **长度因子** — `min(1, len(answer) / 20)`，防止过短回答得高分（权重 20%）

```
score = sim × 0.5 + coverage × 0.3 + length × 0.2
```

score ≥ 0.4 → passed=true

**边界情况：**
- 空输入 → passed=true, score=1.0
- 评估异常 → 降级为纯语义相似度检查

---

## 三、干预引擎 (InterventionEngine)

**文件：** `src/quality/intervention.py`

所有维度评估完成后，汇总 `verdicts`，按优先级匹配规则：

| 优先级 | 维度 | 默认动作 | 说明 |
|--------|------|---------|------|
| 1 (最高) | safety | **block** | 完全替换回答为"已被安全策略拦截"，sources 置空 |
| 2 | factuality | **degrade** | 清空回答文本，仅保留检索来源 |
| 3 | retrieval_quality | **warn** | 在回答末尾追加 ⚠️ 警告提示 |
| 4 | relevance | **warn** | 在回答末尾追加 ⚠️ 警告提示 |

**规则匹配算法：**

```
1. 过滤出 passed=false 的 verdicts
2. 按优先级排序（safety > factuality > retrieval_quality > relevance）
3. 对每个未通过的 verdict，按规则优先级逐一匹配
4. 第一个匹配的规则胜出 → 执行对应 action
5. 全部通过 → action=none（不干预）
```

---

## 四、核心价值

RAG测评本质上是一个**诊断工具**，回答五个问题：

| 问题 | 对应维度 | 实现方式 | 调用 LLM |
|------|---------|---------|----------|
| 检索找到的数据够不够好？ | retrieval_quality | 纯数值统计 | 否 |
| 回答有安全问题吗？ | safety | LLM 语义评判 | 是 |
| 回答是否忠于原文（有无幻觉）？ | factuality | LLM 分解 + jieba 关键词 | 是 |
| 回答是否对题？ | relevance | Embedding + jieba | 否 |
| 回答和标准答案差多远？ | answer_correctness | LLM 分解 + F1 + 语义 | 是（需 ground_truth） |

**成本估算（单次测评）：**
- LLM Judge 调用：3~4 次（safety + factuality + answer_correctness + claim 分解）
- 使用 `deepseek-v4-flash`（轻量模型）控制成本
- 并行执行（`QUALITY_PARALLEL_EVAL=true`），总延迟 ≈ 最慢单个 Judge（~10s）

**典型使用场景：**
- 新增文档后验证 RAG 回答质量
- 排查"回答不对"的用户反馈
- 对比不同检索策略/分块策略的效果
- 调优 Prompt 模板后验证生成质量
