# 企业级 RAG 开发：核心功能与技术全景

---

## 一、数据摄入与处理管道

| 功能 | 技术/方案 |
|------|-----------|
| **多格式解析** | PDF (PyMuPDF/pdfplumber)、Word (python-docx)、PPT、Excel、Markdown、HTML、图片 OCR (PaddleOCR/Tesseract) |
| **版面分析** | Unstructured.io、LayoutParser —— 识别标题/正文/表格/图片，保留阅读顺序 |
| **表格处理** | Table Transformer 检测 + 结构提取，转 Markdown 表格或 JSON |
| **异步/批量摄入** | Celery / BullMQ 任务队列，支持并发解析、限流、重试 |
| **增量同步** | 监听文件变更（inotify/OSS 事件），增量更新向量库而非全量重建 |

---

## 二、分块策略

**这不是简单的按字符数切分，而是企业 RAG 最关键的工程决策之一。**

| 策略 | 说明 |
|------|------|
| **语义分块** | 按 embedding 相似度在相邻句子间找断点，如果两句的 cosine similarity 突降，那里就是分块边界 |
| **层次分块** | 父子文档模型 —— 大块做摘要检索、小块做精确匹配。父块提供上下文，子块精准命中 |
| **滑块窗口 + 重叠** | 固定大小窗口 + overlap，简单但稳定。overlap 一般 10%-25% |
| **文档结构感知** | 按 Markdown 标题层级（`# / ## / ###`）切分，保留章节脉络 |
| **Small-to-Big** | 索引用小粒度块提高相关性，召回后扩展为大块改善生成质量 |

关键技术：`LangChain TextSplitter` 系列、`llama_index IngestionPipeline`、`spaCy` 句子分割

---

## 三、Embedding & 向量存储

| 维度 | 技术选择 |
|------|----------|
| **Embedding 模型** | 中文优先：`bge-large-zh-v1.5`、`m3e-base`；多语言：`text-embedding-3-large`(OpenAI)、`Cohere Embed v3`；本地部署：`BGE` + TEI(Text Embeddings Inference) |
| **向量数据库** | Milvus（海量级）、Qdrant（轻量高性能）、Weaviate（自带向量化）、pgvector（已有 PG 时的零侵入方案） |
| **混合检索** | 向量检索(语义) + BM25/倒排索引(关键词) 融合 —— 避免纯语义检索丢失专有名词/编号 |
| **多向量空间** | 不同业务域用不同 embedding 模型和索引，避免语义混淆 |

---

## 四、检索策略（远不止 Top-K）

| 能力 | 实现方式 |
|------|----------|
| **多路召回 + 融合** | 向量检索 + BM25 + 知识图谱，结果用 RRF(Reciprocal Rank Fusion) 或加权融合排序 |
| **重排序 (Rerank)** | 粗召回后精排：`bge-reranker-v2-m3`、`Cohere Rerank`、`cross-encoder`，对召回结果重新打分 |
| **查询改写 (Query Rewriting)** | 用户问题先让 LLM 改写/拆解/扩展，尤其是模糊简短的问题，提升检索命中率 |
| **HyDE** | 用 LLM 先生成假设性答案，再用这个答案做向量检索 —— 解决 query-document 语义鸿沟 |
| **上下文压缩** | 检索结果经 `Contextual Compression` 或 LLM 去除无关片段后再传给生成模型 |
| **元数据过滤** | 按时间、部门、文档类型、权限标签等元数据缩小检索范围 |
| **Self-Query** | LLM 从用户问题中提取过滤条件，自动构造带元数据过滤的查询 |

---

## 五、生成层

| 功能 | 说明 |
|------|------|
| **Prompt 工程** | System prompt 注入检索到的上下文 + 角色设定 + 约束规则（引用来源、拒绝策略） |
| **引用溯源** | 答案中标注 `[1]`, `[2]` 对应到文档 chunk，前端可点击跳转到原文位置 |
| **流式输出** | SSE (Server-Sent Events) 逐 token 推送，改善用户体验 |
| **防幻觉策略** | "根据提供的资料无法回答" 的明确指令 + confidence score 阈值 + RAG 三元组评估 |
| **多轮对话 RAG** | 指代消解（"那个文件" → 具体哪个）、上下文压缩后作为检索 query |
| **Fallback 链** | 检索无结果 → 改写 query 再搜 → 联网搜索 → 兜底回复 |

---

## 六、知识图谱增强（GraphRAG）

这是高级 RAG 的分水岭：

| 技术点 | 说明 |
|--------|------|
| **实体关系抽取** | 从文档中抽 `(实体, 关系, 实体)` 三元组，构建知识图谱 |
| **图检索** | 基于实体关联做多跳推理，回答 "A 和 B 什么关系" 这种需要跨文档串联的问题 |
| **Graph + Vector 混合** | 向量检索处理事实匹配，图谱处理逻辑关联，两者结果合并 |
| **Microsoft GraphRAG** | 自动构建社区结构，生成社区摘要，适合全局性问题（"这个数据集整体讲了什么"） |

---

## 七、安全与权限

| 需求 | 实现 |
|------|------|
| **文档级权限** | 向量库中每条 chunk 带 `access_group` 标签，检索时注入用户权限过滤 |
| **数据脱敏** | 正则 + NER 脱敏（身份证、手机号、银行卡号）后再入库和返回 |
| **越狱/注入防御** | Prompt injection 检测（输入过滤）、输出审查（敏感词/格式校验） |
| **审计日志** | 每次检索 + 生成全链路记录，包含用户、query、召回文档、生成结果、时间 |
| **租户隔离** | SaaS 场景下每个租户独立 collection/namespace，或向量上带 `tenant_id` 标签 |

---

## 八、评估体系（RAGAS + 自定义）

| 指标维度 | 指标 |
|----------|------|
| **检索质量** | Context Precision、Context Recall、NDCG@K |
| **生成质量** | Faithfulness（忠实度）、Answer Relevance（答案相关性）、Answer Correctness |
| **端到端** | 人工标注 + 自动化评估（RAGAS、TruLens、DeepEval） |

评估是持续优化的前提 —— 没有评估，每一次调参都是盲猜。

---

## 九、可观测性

| 层面 | 技术 |
|------|------|
| **全链路追踪** | LangSmith / LangFuse / Phoenix —— 记录 query → embedding → retrieval → rerank → generation 每一步的耗时和输入输出 |
| **用户反馈闭环** | 点赞/点踩/纠错 → 反馈写入标注集 → 用于定期评估和微调 |
| **成本监控** | Token 用量统计（按模型/用户/天）、向量库存储量、API 延迟 p50/p99 |
| **告警** | 检索召回率突降、生成质量波动、LLM 调用失败率升高 |

---

## 十、Agent 化（RAG → Agent RAG）

RAG 的下一个演进方向是让模型自主决策检索时机和策略：

| 模式 | 说明 |
|------|------|
| **Router Agent** | 根据问题类型路由到不同知识库/检索策略 |
| **ReAct Agent** | Think → Act → Observe 循环，模型自主调用检索工具、决定检索次数 |
| **Multi-Agent** | 不同 Agent 负责不同能力：文档检索 Agent + SQL 查询 Agent + Web 搜索 Agent，由一个协调 Agent 调度 |
| **工具编排** | 检索之外，Agent 还能调 API、执行代码、操作数据库 —— RAG 只是其中一个工具 |

---

## 总结：技术栈一览

```
接入层：FastAPI / Next.js + SSE
管道层：Unstructured + Celery + Redis
分块层：Semantic Chunking + Hierarchical (parent-child)
向量层：bge-large-zh-v1.5 + Milvus/Qdrant + BM25
检索层：Multi-recall + RRF + bge-reranker-v2 + Query Rewriting
生成层：LLM Prompting + 引用溯源 + 流式输出
评估层：RAGAS + LangFuse
安全层：权限过滤 + 脱敏 + 审计
Agent层：LangChain / llama_index Agents + Function Calling
```

按照你项目的当前阶段（已完成上下文管理、分页、摘要化），下一步优先级最高的是 **混合检索（BM25+向量）** 和 **Rerank 重排序**，这两个对最终效果提升最直接。长期目标可以看 **GraphRAG** 和 **Agent 化**。
