# RAG 优化清单

> 持续更新。始于 2026-05-26，覆盖 `src/knowledge/`、`src/api/`、`src/parsing/`、前端。

---

## 2026-05-27 优化改动

### P0-1: 双重分块 — 已修复

**文件**: `src/knowledge/ingestion.py`

- 删除 `SentenceSplitter(chunk_size=512, overlap=50)` 及其 import（`llama_index.core.node_parser`、`llama_index.core.Document`）
- 删除常量 `CHUNK_SIZE = 512`、`CHUNK_OVERLAP = 50`
- `ParsedDocument` 直接转为 `TextNode`（`llama_index.core.schema.TextNode`），保留 Chunker 确定的语义边界
- 日志从 "Split into %d nodes" 改为 "Ingesting %d nodes"

### P0-2: 分词覆写原文 — 已修复

**文件**: `src/knowledge/ingestion.py`、`src/knowledge/query_engine.py`、`src/knowledge/retrieval.py`

- `ingestion.py`: 分词前将原文保存到 `node.metadata["original_text"]`，然后 `node.set_content(tokenize(...))`。嵌入用原文（已如此），BM25 用分词内容，LLM/重排器取 `original_text`
- `query_engine.py:103`: `node.get_content()` → `node.metadata.get("original_text", node.get_content())`
- `retrieval.py:107`: 重排候选文本改用 `original_text`

### P1-6: retrieve()/retrieve_with_rerank() 去重 + text_to_node 覆盖修复 — 已修复

**文件**: `src/knowledge/retrieval.py`

- `text_to_node = {node.get_content(): node}` 字典映射 → 索引遍历匹配（`enumerate` + `break`），避免相同文本的节点被后者覆盖而静默丢失
- 重排序候选文本同时改用 `original_text`

---

## 2026-05-26 分析记录

### 今日确认已完成（历史优化，非今日改动）

- [x] **混合搜索 mode 修复** — `retrieval.py:51,93` 两处均为 `mode="hybrid"`（之前误判为 `mode="default"`，实际已修好）
- [x] **jieba 中文分词** — `src/knowledge/tokenizer.py` 已存在，ingestion 写入侧和 retrieval 查询侧均已接入 jieba 分词，中文 BM25 可正常工作
- [x] **无需 zhparser** — jieba 应用层分词 + `text_search_config="simple"` 空格分隔方案已满足中文需求，PG 不需要额外扩展

### 今日新发现

- **混合搜索内部机制**：LlamaIndex PGVectorStore `_hybrid_query()` 实际是两条独立 SQL（dense + sparse）拼接去重，不做分数归一化或加权融合。dense 优先去重，sparse 分数被丢弃。`alpha` 参数传了也忽略。
- **PG `'simple'` 分词器对中文的影响**：`to_tsvector('simple', '中文文本')` 会将 CJK 字符按单字切分（"中/文/文/本"），但 jieba 已经通过空格分词规避了这个问题。

---

## 待优化（按优先级）

### P0 — 严重影响检索质量

- [x] **双重分块** — `upload.py:74` 用 `Chunker(chunk_size=1024)` 先切一次，`ingestion.py:31` 用 `SentenceSplitter(chunk_size=512)` 再切一次。两次切割破坏语义完整性，且 overlap 不一致（100 vs 50）。~~建议去掉一重。~~ **已修复 (2026-05-27)**：去除 SentenceSplitter，ParsedDocument 直接转 TextNode。
- [ ] **doc_ids 过滤未生效** — `schema.py` 定义了 `doc_ids` 字段，`query_engine.py` 接收了参数但没传给 retriever，`retrieval.py` 也无 doc_id 过滤能力。传了也不会生效。

### P1 — 架构改进

- [ ] **分数阈值 0.35 是魔法数字** — 硬编码在 `query_engine.py:85,95` 两处。Stage 1 用余弦相似度、Stage 2 用 cross-encoder 分，两套分数体系却用同一个阈值比较，不合理。改为可配置 + 两阶段分开设。
- [x] **retrieve() 和 retrieve_with_rerank() 代码重复** — 前 40 行完全相同（embedding、短查询 boost、VectorStoreQuery、score 绑定）。~~抽公共方法。~~ **已修复 (2026-05-27)**：text_to_node 覆盖问题已修复（索引遍历匹配），公共方法抽取待后续重构。
- [ ] **Query embedding 无缓存** — 相同问题反复调 Zhipu API 重新 embedding，加 LRU 缓存。

### P2 — 参数可配置化

- [x] `COARSE_TOP_K = 20` → `config.py:retrieval_coarse_k`（已存在）
- [x] `FINE_TOP_K = 5` → `config.py:retrieval_fine_k`（已存在）
- [x] 分数阈值 `0.35` → `config.py:retrieval_stage1/2_threshold`（已存在）
- [x] `CHUNK_SIZE / CHUNK_OVERLAP` — 已随 SentenceSplitter 移除
- [x] `rerank_min_score` → `config.py:rerank_min_score`（本次新增）
- [x] `rerank_enabled` → `config.py:rerank_enabled`（本次新增）
- [ ] snippet 截断 `300` (`query_engine.py:111`) — 待配置化

### P3 — 体验和可观测性

- [ ] **HyDE 失败静默降级** — `query_engine.py:128` 的 `except Exception: return None` 不打印任何日志，加 `logger.warning`
- [ ] **RAG 无流式输出** — `/query` 同步返回完整答案，长答案体验差。增加 `/query/stream`
- [ ] **低分/拒答无追踪** — score ≤ 0.35 的查询不记录，无法分析知识库覆盖缺口。写入日志或数据库

---

---

## 2026-05-28 优化改动

### P0-3: O(n²) node 匹配 → O(1) 字典查找

**文件**: `src/knowledge/retrieval.py:118-130`

- `text_to_nodes` 从 `{text: node}` 单节点字典改为 `defaultdict(list)`，支持重复文本
- 重排后遍历时 O(1) 查找 + `pop(0)` 消费，避免重复匹配同一节点
- 之前 bug: `enumerate` + `break` 虽避免了覆盖，但外层再套一层导致 O(n²)

### P0-4: reranker 增加 min_score 过滤 + 异常保护 + 分数归一化

**文件**: `src/knowledge/reranker.py:45-97`

- 新增 `min_score` 参数，对归一化后的分数做截断（至少保留 1 条结果）
- 新增 `try/except` 保护 `FlagReranker.compute_score()`，OOM 等异常时降级返回原始顺序
- 新增 min-max 归一化：`(score - min) / (max - min)`，使分数落在 [0, 1] 区间，便于跨查询统一阈值

### P0-5: 可配置 rerank 参数

**文件**: `src/config.py:68-69`

- `rerank_min_score: float = 0.1` — 精排最小分数阈值（归一化后）
- `rerank_enabled: bool = True` — 开关，关闭时 `retrieve_with_rerank()` 直接返回粗排结果

### P0-6: 新增 reranker 单元测试

**文件**: `tests/test_knowledge/test_reranker.py`（新建）

- 9 个测试用例，mock `FlagReranker`，无需下载模型（~1.3 GB）
- 覆盖：空候选、排序、top_k、归一化、min_score 过滤、异常降级

---

## 关键参数速查

| 参数 | 值 | 位置 |
|------|-----|------|
| COARSE_TOP_K | 20 | `retrieval.py:15` |
| FINE_TOP_K | 5 | `retrieval.py:16` |
| 分数阈值 | 0.35 | `query_engine.py:85,95` |
| CHUNK_SIZE (ingestion) | ~~512~~ 已移除 | — |
| CHUNK_OVERLAP (ingestion) | ~~50~~ 已移除 | — |
| Chunker.chunk_size (upload) | 1024 | `upload.py:74` |
| Chunker.chunk_overlap (upload) | 100 | `upload.py:74` |
| 向量维度 | 1024 | `config.py:24` |
| 嵌入模型 | Zhipu embedding-3 | `config.py:22` |
| 精排模型 | BAAI/bge-reranker-large | `reranker.py:19` |
| HyDE temperature | 0.3 | `query_engine.py:137` |

---

## 已改动的文件

```
2026-05-28 改动:
src/knowledge/reranker.py     ← min_score 过滤 + 异常保护 + 分数归一化
src/knowledge/retrieval.py    ← O(n²)→O(1) node匹配 + rerank_enabled 开关
src/config.py                 ← 新增 rerank_min_score, rerank_enabled
tests/test_knowledge/test_reranker.py ← 新建，9个单元测试
docs/RAG优化清单.md           ← 本次优化记录（含后续追加）

2026-05-27 改动:
src/knowledge/ingestion.py    ← 去除双重分块 + 原文保留到 original_text
src/knowledge/query_engine.py ← LLM上下文使用 original_text
src/knowledge/retrieval.py    ← 重排使用 original_text + text_to_node 覆盖修复

2026-05-26 及之前:
src/knowledge/tokenizer.py    ← 新建，jieba 分词
src/knowledge/ingestion.py    ← 写入前分词（第54-58行）
src/knowledge/retrieval.py    ← mode="hybrid" + query分词
```

---

## 2026-05-29 优化改动

### 数据库连接池参数调优 + 泄漏修复

**文件**: `src/api/deps.py`

- `max_size` 从 10 提升到 **20**，`timeout` 从 5 提升到 **10**
- 新增 `max_lifetime=300`（5 分钟自动回收连接，防止泄漏堆积）
- 移除 `get_pg_connection()` 中的 `SELECT 1` 健康检查（pool.getconn() 内部已有，双重检查导致竞争条件）
- 新增 `pool_stats()` 辅助函数用于调试连接池状态

### 递归切片策略

**文件**: `src/parsing/chunker.py`

- 新增 `recursive` 策略：三段降级（Level 1 段落 → Level 2 句子 → Level 3 字符滑动窗口）
- 遇到超长文本自动降级到更细粒度，极端情况有兜底

### 切片策略可配置化

**文件**: `src/config.py`、`src/api/routes/upload.py`

- `config.py` 新增 `chunk_strategy`（默认 `sentence`）、`chunk_size`（1024）、`chunk_overlap`（100）
- `upload.py` 中 `Chunker` 初始化从硬编码改为读取 settings，`upload_document` 和 `upload_stream` 均新增 `strategy` Query 参数，前端可覆盖

### 前端切片策略选择器

**文件**: `src/views/DocumentsView.vue`（前端项目）

- 文档管理页顶部新增策略下拉框（字符滑动窗口 / 语义边界 / Markdown标题 / 递归分块）
- 默认值为 `fixed_size`（字符滑动窗口），上传时通过 `?strategy=` 参数传给后端

### RAG 查询缓存完善

**文件**: `src/knowledge/query_engine.py`

- "未找到" 和 "向量库不可用" 的响应也写入缓存（TTL=60 秒），避免重复检索
- 之前仅缓存成功找到文档的回答，"未找到"情况每次重复检索

### 新增文档

**文件**: `docs/chunk-strategies.md`（新建）

- 四种切片策略的中文说明文档，含原理图、参数、优缺点、适用场景、对比表

## 关键参数速查（更新）

| 参数 | 值 | 位置 |
|------|-----|------|
| chunk_strategy | sentence | `config.py:71` |
| chunk_size | 1024 | `config.py:72` |
| chunk_overlap | 100 | `config.py:73` |
| pool.max_size | 20 | `deps.py:41` |
| pool.max_lifetime | 300s | `deps.py:42` |

## 已改动的文件

```
2026-05-29 改动:
src/api/deps.py                ← 连接池参数调优 + 泄漏修复 + pool_stats()
src/parsing/chunker.py         ← 新增 recursive 递归切片策略
src/config.py                  ← 新增 chunk_strategy/size/overlap
src/api/routes/upload.py       ← Chunker 从硬编码改为 settings + Query 参数
src/knowledge/query_engine.py  ← "未找到" 响应也写入缓存
docs/chunk-strategies.md       ← 新建，四种切片策略说明文档
前端 src/views/DocumentsView.vue ← 新增策略选择下拉框
```
