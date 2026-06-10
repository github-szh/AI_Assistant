# 完整 RAG 链路 — 逐步骤讲解

> 示例问题：`"什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？"`

---

## 第 1 步：前端发起请求

**文件**：`ChatView.vue` 第 204-211 行

```javascript
const ragResp = await fetch('/api/query/stream', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
        question: "什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？",
        top_k: 3,
        session_id: "151b74b278ad4d69",
        messages: [...],  // 对话历史
    }),
})
```

用户点击发送 → 前端 POST 一个 JSON 到 `/api/query/stream`。

---

## 第 2 步：API 路由接收，提取用户身份

**文件**：`src/api/routes/query.py` 第 65-79 行

```python
@router.post("/stream")
async def query_knowledge_stream(
    req: QueryRequest,
    engine: QueryEngine = Depends(get_query_engine),
    user: dict = Depends(require_permission("knowledge:query")),
):
    async def generate():
        async for sse_line in engine.query_stream(
            question="什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？",
            top_k=3,
            doc_ids=None,
            messages=[...],
            tenant_id=get_effective_tenant_id(user),  # SuperAdmin→None, 普通用户→1
        ):
            yield sse_line
    return StreamingResponse(generate(), media_type="text/event-stream")
```

做的事：
- `require_permission("knowledge:query")` 从 JWT 解码出 `{user_id, username, role, tenant_id}`
- `get_effective_tenant_id()` 判断：SuperAdmin 返回 `None`（不过滤租户），其他人返回自己的 tenant_id
- 调用 `engine.query_stream()`，把结果用 SSE（Server-Sent Events）流式返回

---

## 第 3 步：query_stream 入口

**文件**：`src/knowledge/query_engine.py` 第 346-361 行

```python
async def query_stream(self, question, top_k=3, doc_ids=None, messages=None, tenant_id=None):
    # ① 调用检索，拿到 nodes + sources + context
    result = await self._retrieve(question, top_k, doc_ids, messages, tenant_id)

    if result is None:
        yield "向量数据库未就绪"  # 向量库挂了
        return

    if not result["nodes"]:
        yield "知识库中没有找到相关信息"  # 没有召回任何东西
        return

    # ② 发送检索步骤给前端（展示检索流水线）
    yield f"data: {json.dumps({'steps': result['steps']})}\n\n"

    # ③ 发送置信度
    yield f"data: {json.dumps({'status': 'found', 'confidence': confidence})}\n\n"

    # ④ 调 LLM 生成答案（流式），逐 token 发送
    # ...

    # ⑤ 最后发送来源列表
    yield f"data: {json.dumps({'sources': sources, 'confidence': confidence})}\n\n"
```

核心是 `_retrieve()` —— 这是整个检索链路。

---

## 第 4 步：_retrieve — 问题改写

**文件**：`src/knowledge/query_engine.py` 第 70-99 行

```python
def _rewrite_question(self, question, messages):
    if not messages or len(messages) <= 1:
        return question  # 没有历史，不改写
    # 有历史 → 让 LLM 把追问改写成独立问题
    llm.chat("根据对话历史，将用户的追问改写为一个独立的、不依赖上下文的查询问题...")
```

对于我们的示例问题（这是一个完整的独立问题，不是追问），假设无对话历史，`_rewrite_question` 直接返回原文：

```
"什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？"
```

---

## 第 5 步：Level 1 — 文档摘要搜索

**文件**：`src/knowledge/query_engine.py` 第 129-148 行

```python
# 如果知识库文档 >= 10 篇，先走 Level 1 缩小范围
if doc_count >= settings.two_stage_min_docs:  # 默认 10
    # ① 把问题转成向量（调用智谱 embedding-3 API）
    query_emb = emb_mgr.encode_query(search_question)

    # ② 在 doc_summaries 表里搜最相似的文档摘要
    relevant = _search_summaries(query_emb, top_k=5, tenant_id=tenant_id)

    # ③ 拿到相关的 doc_id 列表
    target_ids = [r["doc_id"] for r in relevant]
```

**文件**：`src/knowledge/index_store.py` 第 198-233 行

```python
def _search_summaries(query_embedding, top_k=5, tenant_id=None):
    conn.execute("""
        SELECT doc_id, summary, filename, chunk_count,
               1 - (embedding <=> %s::vector) AS similarity
        FROM doc_summaries
        WHERE tenant_id = %s
        ORDER BY embedding <=> %s::vector   -- pgvector 余弦距离排序
        LIMIT %s
    """, [query_embedding, tenant_id, query_embedding, top_k])
```

用到的 PostgreSQL 算子：
- `<=>` 是 pgvector 的**余弦距离**算子，值越小越相似
- `1 - 距离 = 相似度`，范围 [0, 1]

对于我们的问题，Level 1 预计会选出：

```
doc_id=408cf109a579 (劳动法.pdf)    — 摘要含"解除、经济补偿"
doc_id=758ba0fc8dc3 (公司规则制度.pdf) — 摘要含"解除劳动合同、薪资"
```

`target_ids` = `["408cf109a579", "758ba0fc8dc3"]`，后续 Level 2 只在这 2 篇文档里搜。

---

## 第 6 步：Level 2a — 向量召回（Dense Retrieval）

**文件**：`src/knowledge/retrieval.py` 第 230-280 行

```python
async def _coarse_retrieve(self, query, doc_ids=None, tenant_id=None):
    # ① 生成 query 的向量
    query_embedding = embed_mgr.encode_query(query)

    # ② 构建 metadata 过滤条件
    filters = MetadataFilters(filters=[
        MetadataFilter(key="source", operator=FilterOperator.IN, value=target_ids),
        MetadataFilter(key="tenant_id", operator=FilterOperator.EQ, value=str(tenant_id)),
    ], condition=FilterCondition.AND)

    # ③ 调 pgvector 向量搜索
    q = VectorStoreQuery(
        query_embedding=query_embedding,
        similarity_top_k=40,  # coarse_k: 召回 40 条候选
        filters=filters,
    )
    result = store.query(q)
    dense_nodes = result.nodes  # 40 条向量搜索结果
```

pgvector 内部执行的是（由 LlamaIndex 封装）：

```sql
SELECT node_id, text, metadata_,
       1 - (embedding <=> $1::vector) AS similarity
FROM data_documents
WHERE metadata_->>'source' IN ('408cf109a579', '758ba0fc8dc3')
  AND metadata_->>'tenant_id' = '1'
ORDER BY embedding <=> $1::vector
LIMIT 40;
```

返回的 `dense_nodes` 每个节点上挂了一个 `score`（余弦相似度，0~1）。

---

## 第 7 步：Level 2b — BM25 关键词召回（Sparse Retrieval）

**文件**：`src/knowledge/tokenizer.py` 第 35-52 行

首先，用 jieba 把中文问题分词：

```python
def tokenize(text):
    # "什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？"
    #     ↓ jieba 分词
    # "什么 情况 下 解除 劳动 合同 公司 需要 支付 经济 补偿 补偿 标准 如何 计算"
    words = jieba.cut(text)
    return " ".join(words)
```

**文件**：`src/knowledge/retrieval.py` 第 33-83 行

```python
async def _bm25_search(query_tokens, top_k=40, doc_ids=None, tenant_id=None):
    # ① 过滤掉无意义 token，构建 OR 连接的 tsquery
    tokens = [t for t in query_tokens.split() if len(t) > 1 or t.isalnum()]
    tsquery = " | ".join(tokens)
    # 结果类似: "解除 | 劳动 | 合同 | 公司 | 需要 | 支付 | 经济 | 补偿 | 补偿 | 标准 | 如何 | 计算"

    # ② 执行 PostgreSQL 全文搜索
    conn.execute("""
        SELECT node_id, text, metadata_,
               ts_rank(to_tsvector('simple', text), to_tsquery('simple', %s)) AS rank
        FROM data_documents
        WHERE to_tsvector('simple', text) @@ to_tsquery('simple', %s)
          AND metadata_->>'source' IN ('408cf109a579', '758ba0fc8dc3')
          AND metadata_->>'tenant_id' = '1'
        ORDER BY rank DESC
        LIMIT 40
    """, [tsquery, tsquery])
```

PostgreSQL 的 `ts_rank` 计算 TF-IDF 风格的文档-查询相关性分数。返回 `bm25_score` 越高越相关。

---

## 第 8 步：RRF 融合（Reciprocal Rank Fusion）

**文件**：`src/knowledge/retrieval.py` 第 88-129 行

把向量搜索和 BM25 搜索的结果**合并成一份排名**。

**核心公式**：

```
RRF_score(node) = Σ ( 1 / (k + rank_i) )

其中:
  k = 60 (经验常数)
  rank_i = 节点在第 i 个排序列表中的位置 (0-indexed)
```

**具体计算示例**：

假设某 chunk 在向量搜索结果排第 2 名（rank=1），BM25 结果排第 5 名（rank=4）：

```
RRF_score = 1/(60+1) + 1/(60+4)
          = 1/61 + 1/64
          = 0.0164 + 0.0156
          = 0.0320
```

**代码实现**：

```python
def _rrf_fusion(dense_nodes, sparse_results, k=60, top_k=40):
    scores = {}
    node_map = {}

    # 向量搜索贡献
    for rank, node in enumerate(dense_nodes):
        nid = node.node_id
        scores[nid] = scores.get(nid, 0) + 1.0 / (60 + rank + 1)
        node_map[nid] = node

    # BM25 贡献
    for rank, row in enumerate(sparse_results):
        nid = row["node_id"]
        scores[nid] = scores.get(nid, 0) + 1.0 / (60 + rank + 1)

    # 按 RRF 分数从高到低排序，取 top 40
    sorted_ids = sorted(scores, key=lambda nid: scores[nid], reverse=True)
    return [node_map[nid] for nid in sorted_ids[:40]]
```

**为什么用 RRF？** 向量搜索偏好语义相似，BM25 偏好关键词匹配。同一个 chunk 在两边都排名靠前 → RRF 分数高 → 说明它"双重确认"地相关。

**关键点**：融合完后，最大向量相似度（`max_dense`）被单独保存到 `node._dense_max_score`，后续用来判断走快路径还是深度搜索。

---

## 第 9 步：Sentence Window 展开

**文件**：`src/knowledge/retrieval.py` 第 134-206 行

```python
def _expand_to_parents(child_nodes, tenant_id=None):
    for node in child_nodes:
        pid = node.metadata.get("parent_id")
        if pid:
            parent_ids.append(pid)

    # 批量从 chunk_contexts 表查父块
    parents = _fetch_parent_contexts(parent_ids, tenant_id=tenant_id)

    # 把子块替换成父块（保留子块的分数）
    parent = TextNode(text=data["content"], ...)
    object.__setattr__(parent, 'score', getattr(node, 'score', 0))
```

大白话：搜索用小块（精确匹配），展示给 LLM 用大块（完整上下文）。比如搜到第 47 条法律的第 3 段，展开后给 LLM 的是整条法律。

---

## 第 10 步：快路径 vs 深度搜索判断

**文件**：`src/knowledge/query_engine.py` 第 163-193 行

```python
top_score = getattr(nodes[0], "_dense_max_score", None)  # 向量最大相似度

if top_score <= settings.retrieval_stage1_threshold:  # 默认 0.65
    # 深度搜索：相似度不够高，走 HyDE + Reranker
    logger.info("深度搜索: max_dense=%.3f ≤ 0.65 → HyDE+Reranker", top_score)
    deep_search_used = True
else:
    # 快路径：向量搜索足够好了，直接用
    logger.debug("快路径: 跳过深度搜索")
```

`top_score` 是向量搜索的**余弦相似度**（不是说 RRF 分数）。如果最高相似度 > 0.65，说明找到的内容足够相关，跳过昂贵的 Reranker。否则走深度搜索。

对于我们的问题，假设文档内容和问题匹配度一般（laws 和 company policy 里确实有"解除劳动合同""经济补偿"这些词的常见出现），top_score 可能 **< 0.65**，触发深度搜索。

---

## 第 11 步：HyDE — 假设答案生成

**文件**：`src/knowledge/query_engine.py` 第 398-412 行

```python
def _generate_hypothetical(self, question):
    llm.chat(
        "请用一段话（50-100字）回答以下问题。不需要真实准确，只需要写出一个"
        "看起来像答案的段落，用于帮助搜索引擎找到相关文档。\n"
        "问题：什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？"
    )
```

LLM 可能生成类似这样的假设答案：

> "根据劳动合同法规定，用人单位在以下几种情况下需要支付经济补偿：劳动者因用人单位过错解除合同、用人单位提出协商解除、用人单位裁员或破产。补偿标准按劳动者在本单位工作年限，每满一年支付一个月工资，六个月以上不满一年的按一年计算，不满六个月的支付半个月工资。"

这个"假答案"用更专业的词（"用人单位""裁员""工作年限"），用它去搜比直接用口语问题效果好。

---

## 第 12 步：BGE Reranker 深度重排序

**文件**：`src/knowledge/reranker.py` 第 28-93 行

重排序器是独立进程（避免和 LlamaIndex 的 HuggingFace 库冲突），通过 stdin/stdout 通信：

```python
# 主进程发送请求
self._proc.stdin.write(json.dumps({
    "query": "用人单位在以下几种情况下需要支付经济补偿...",  # HyDE 生成的假答案
    "candidates": [
        "第四十六条 有下列情形之一的，用人单位应当向劳动者支付经济补偿...",  # chunk 1
        "严重违纪包括连续旷工、侵占财物或泄密将直接解约...",               # chunk 2
        "...",
    ],
    "top_k": 5,
}))

# Worker 子进程加载 bge-reranker-v2-m3 模型，对每个 (query, chunk) 打分
# 返回: [("chunk文本", 3.21), ("chunk文本", 1.15), ...]
```

BGE Reranker 是一个 Cross-encoder：把 (问题, 文档) **成对输入**模型，输出一个原始分数（logit），比向量搜索的余弦相似度精确得多。代价是慢——每对都要过一次模型。

---

## 第 13 步：Min-Max 归一化

**文件**：`src/knowledge/retrieval.py` 第 369-380 行

BGE 输出的 logit 是**无界**的（可能是 -5 到 +10），需要归一化到 [0, 1]：

```python
if reranked_nodes:
    all_scores = [getattr(n, 'score', 0.0) for n in reranked_nodes]
    s_min, s_max = min(all_scores), max(all_scores)
    for n in reranked_nodes:
        s = getattr(n, 'score', 0.0)
        if s_max > s_min:
            # 多条结果：min-max 归一化
            n.score = (s - s_min) / (s_max - s_min)
        else:
            # 单条结果：sigmoid 映射 (因为 min==max 分母为 0)
            n.score = round(1 / (1 + math.exp(-s)), 4)
        n._reranked = True  # 标记为已重排序
```

**示例**：假设 reranker 输出 3 个分数 `[3.2, 1.1, -0.5]`：

```
最高分: (3.2 - (-0.5)) / (3.2 - (-0.5)) = 3.7 / 3.7 = 1.0
中间分: (1.1 - (-0.5)) / 3.7 = 1.6 / 3.7 = 0.43
最低分: (-0.5 - (-0.5)) / 3.7 = 0 / 3.7 = 0.0
```

---

## 第 14 步：构建上下文 + 置信度分级

**文件**：`src/knowledge/query_engine.py` 第 202-261 行

```python
# 把 top_k 条结果拼成 prompt 上下文
context_parts = []
for i, node in enumerate(top_nodes):
    fname = node.metadata.get("filename", "")
    content = node.metadata.get("original_text")
    context_parts.append(f"[{i+1}] ({fname})\n{content}")

context = "\n\n".join(context_parts)
```

然后计算置信度：

```python
if deep_search_used:
    if top_score >= 0.70:    confidence = "high"
    elif top_score >= 0.50:  confidence = "medium"
    else:                    confidence = "low"
else:
    if top_score >= 0.70:    confidence = "high"
    else:                    confidence = "medium"  # 快路径至少 medium
```

---

## 第 15 步：构建 Prompt + LLM 生成

**文件**：`src/knowledge/query_engine.py` 第 414-416 行 + `src/utils/prompt_loader.py` 第 13-34 行 + `prompts/rag/query.yaml`

```python
def _build_prompt(self, question, context):
    return load_prompt("rag/query", context=context, question=question)
```

这是把 YAML 模板 + 检索到的上下文 + 用户问题拼在一起：

```
根据以下参考资料回答用户问题。

要求：
1. 每个事实陈述后面标注来源编号 [来源:N]
...

参考资料：
[1] (劳动法.pdf)
第四十六条 有下列情形之一的，用人单位应当向劳动者支付经济补偿：
（一）劳动者依照本法第三十八条规定解除劳动合同的；
（二）用人单位依照本法第三十六条规定向劳动者提出解除劳动合同...
...

[2] (公司规则制度.pdf)
奖惩制度明确了从口头表扬到解除劳动合同的具体情形...

用户问题：什么情况下解除劳动合同公司需要支付经济补偿？补偿标准如何计算？

回答：
```

然后调 LLM 生成，逐 token 流式返回给前端。

---

## 完整链路总览图

```
用户点击发送
    │
    ▼
ChatView.vue (L204) ─── POST /api/query/stream ───►
    │
    ▼
query.py (L74) ─── 解码 JWT, 提取 tenant_id ───►
    │
    ▼
query_engine.py (L350) ─── query_stream() ───►
    │
    ├─ L116: _rewrite_question()       ← 追问改写 (无历史则跳过)
    │
    ├─ L140: _search_summaries()       ← Level 1: 文档摘要向量搜索
    │         index_store.py (L203)     ← SELECT ... FROM doc_summaries
    │                                      ORDER BY embedding <=> query
    │
    ├─ L152: _coarse_retrieve()        ← Level 2: 混合召回
    │         retrieval.py (L222)
    │           │
    │           ├─ (L238) store.query() ← 向量搜索 (pgvector)
    │           │         40 条 dense nodes
    │           │
    │           ├─ (L286) _bm25_search()← BM25 关键词搜索
    │           │         jieba 分词 → ts_rank → 40 条 sparse results
    │           │
    │           └─ (L294) _rrf_fusion()← RRF 融合
    │                     score = Σ 1/(60+rank)  → top 40
    │
    ├─ L330: _expand_to_parents()      ← Sentence Window 展开
    │         子块 → 父块 (完整上下文)
    │
    ├─ L172: 阈值判断 ←──────────────┐
    │   top_score > 0.65?            │
    │   ├─ 是 → 快路径, 直接进入 LLM  │
    │   └─ 否 → 深度搜索 ─────────────┘
    │           │
    │           ├─ (L176) _generate_hypothetical()  ← HyDE 生成假答案
    │           │
    │           └─ (L182) retrieve_with_rerank()    ← BGE Reranker 精排
    │                     reranker.py (L56)
    │                       │
    │                       ├─ Cross-encoder 打分
    │                       └─ Min-Max 归一化 [0,1]
    │
    ├─ L195: top_k=3  ← 取前 3 条
    │
    ├─ L202: 构建 context + SourceInfo
    │
    ├─ L224: 置信度分级 (high/medium/low)
    │
    └─ L369: _build_prompt() → LLM 流式生成 → 返回答案 + 来源
```

---

## 各环节耗时参考（以"claude code使用"为例）

从日志第 21752-21772 行：

| 环节 | 耗时 |
|------|------|
| 问题改写（LLM） | 1.83s |
| 向量搜索（40 条） | 0.06s |
| BM25 搜索（40 条） | 0.45s |
| Sentence Window 展开 | 0.02s |
| HyDE 生成（LLM） | 3.20s |
| BGE Reranker（15→5） | 47.39s |
| LLM 生成回答 | 2.53s |
| **总耗时** | **~55s** |

Reranker 是最慢的一环（47 秒），因为 BGE-reranker-v2-m3 模型在 CPU 上跑交叉编码。有 GPU 的话这个时间能压到 1-2 秒。
