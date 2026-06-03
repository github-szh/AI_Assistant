# AI Assistant — 项目架构文档

> 生成日期：2025-05-19 | 代码量：~2800 行 Python + Vue 3 前端

---

## 一、总体架构图

```
┌──────────────────────────────────────────────────────────┐
│                      前端 (Vue 3)                         │
│  /login         /chat (对话+自动RAG)    /documents (管理) │
│  PostMessage    SSE 流式消费             Axios ↔ API      │
└────────────────────┬─────────────────────────────────────┘
                     │ HTTP (Vite proxy /api → :8000)
┌────────────────────┴─────────────────────────────────────┐
│                   FastAPI 后端                            │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐           │
│  │ auth.py  │  │ chat.py  │  │ upload.py   │           │
│  │ 注册/登录 │  │ 对话/流式 │  │ 上传/解析/入库│           │
│  └──────────┘  └──────────┘  └──────────────┘           │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐           │
│  │sessions  │  │ query.py │  │ documents.py │           │
│  │ 会话CRUD │  │ RAG问答  │  │ 文档列表/详情  │           │
│  └──────────┘  └──────────┘  └──────────────┘           │
│                                                          │
│  ┌──────────────────────────────────────────┐           │
│  │              LLM Router                   │           │
│  │  deepseek → zhipu → openai → mock        │           │
│  │  重试 + 降级链                             │           │
│  └──────────────────────────────────────────┘           │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐      │
│  │ RAG Engine  │  │ Doc Parsing  │  │ Prompt Mgr │      │
│  │ HyDE+检索   │  │ PDF/OCR/DOCX │  │ YAML+变量   │      │
│  │ +精排+生成  │  │ +Chunker     │  │ 注入        │      │
│  └─────────────┘  └──────────────┘  └────────────┘      │
└────────────────────┬─────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   ┌─────────┐ ┌─────────┐ ┌──────────┐
   │PostgreSQL│ │  Redis  │ │   Disk   │
   │t_user    │ │ Session │ │ documents│
   │t_session │ │ Cache   │ │ models/  │
   │ pgvector │ │ Rate Lmt│ │ md5.json │
   │data_docs │ │         │ │ prompts/ │
   └─────────┘ └─────────┘ └──────────┘
```

---

## 二、目录结构与职责

```
AI_Assistant/
├── src/
│   ├── config.py                         # 全局配置 (Pydantic Settings)
│   │
│   ├── llm/                              # LLM 路由层
│   │   ├── base.py                       # BaseLLMProvider 抽象基类
│   │   ├── router.py                     # LLMRouter (选择+重试+降级)
│   │   └── providers/
│   │       ├── deepseek.py               # DeepSeek (含 V4 thinking 控制)
│   │       ├── zhipu.py                  # 智谱 GLM (zai SDK)
│   │       ├── openai.py                 # OpenAI 备选
│   │       └── mock.py                   # 测试 mock
│   │
│   ├── parsing/                          # 文档解析层
│   │   ├── loader.py                     # DocumentLoader (格式路由+OCR检测)
│   │   ├── pdf_text.py                   # PyMuPDF 文字型 PDF
│   │   ├── pdf_markdown.py               # Marker 复杂排版
│   │   ├── ocr.py                        # PaddleOCR 扫描件/图片
│   │   ├── office_parser.py              # DOCX + PPTX
│   │   ├── cloud_parse.py                # llama-parse 云解析
│   │   └── chunker.py                    # 文本分块 (fixed/sentence/markdown)
│   │
│   ├── knowledge/                        # RAG 知识库
│   │   ├── embeddings.py                 # bge-large-zh-v1.5 管理器
│   │   ├── index_store.py                # pgvector + ChromaDB 双模
│   │   ├── ingestion.py                  # Document → Pipeline → VectorStore
│   │   ├── retrieval.py                  # 混合检索 (BM25+向量, 动态权重)
│   │   ├── reranker.py                   # bge-reranker-large 精排
│   │   └── query_engine.py               # RAG 编排 (HyDE → 检索 → LLM)★
│   │
│   ├── api/                              # FastAPI 服务层
│   │   ├── main.py                       # 应用入口 + 路由注册
│   │   ├── schemas.py                    # Pydantic 请求/响应模型
│   │   ├── middleware.py                 # 日志+计时中间件
│   │   ├── deps.py                       # 依赖注入
│   │   └── routes/
│   │       ├── auth.py                   # 注册/登录/JWT
│   │       ├── chat.py                   # /chat (阻塞式 + 会话存消息)
│   │       ├── chat_stream.py            # /chat/stream (SSE 流式)
│   │       ├── sessions.py               # 会话 CRUD (PG 持久化)
│   │       ├── query.py                  # /query (RAG 问答)
│   │       ├── upload.py                 # /upload (解析+入库+摘要+去重)★
│   │       ├── documents.py              # /documents (列表+详情)★
│   │       ├── delete_document.py        # DELETE /documents/{id}
│   │       └── health.py                 # /health
│   │
│   ├── storage/                          # 存储层
│   │   └── cache.py                      # Redis 会话缓存 + 限流器
│   │
│   ├── agent/                            # Agent 预留
│   │   └── tools/
│   │
│   ├── observability/                    # 观测性
│   │   ├── logging_config.py             # 日志配置
│   │   └── tracing.py                    # OpenTelemetry 占位
│   │
│   └── utils/
│       └── prompt_loader.py              # YAML prompt 加载器 + 变量注入
│
├── prompts/                              # Prompt 模板 (YAML)
│   ├── assistant.yaml                    # 系统角色定义
│   ├── rag/query.yaml                    # RAG 问答模板
│   └── agent/react.yaml                  # Agent 模板 (预留)
│
├── scripts/
│   ├── init_pgvector.sql                 # PG 初始化 SQL
│   ├── download_models.py                # 模型下载 (ModelScope)
│   ├── test_pg_connection.py             # PG 连接诊断
│   ├── generate_test_data.py             # 测试数据生成
│   └── seed_data.py                      # 数据灌入
│
├── tests/                                # 21 个单元测试
├── data/
│   ├── documents/                        # 上传文件存储
│   ├── models/                           # bge 模型 (gitignored, ~2.6GB)
│   ├── md5_store.json                    # 文件去重记录
│   └── chroma_db/                        # ChromaDB 降级存储
│
├── start.bat                             # 一键启动脚本
├── FEATURES.md                           # 功能说明文档
├── ARCHITECTURE.md                       # 本文档
└── pyproject.toml                        # Python 依赖
```

---

## 三、核心数据流

### 3.1 对话流程 (`POST /chat`)
```
Frontend → /chat (ChatRequest)
  → LLMRouter.chat()
    → DeepSeekProvider.chat() (或 fallback)
    → OpenAI SDK → DeepSeek API
  → 返回 ChatResponse
  → 若带 session_id → 自动写 t_session_message
  → 自动命名会话 (首条user消息前10字)
```

### 3.2 流式对话 (`POST /chat/stream`)
```
Frontend → fetch /chat/stream
  → SSE event_generator()
    → OpenAI SDK stream=True
    → for chunk in response:
        yield "data: {c: 'token'}\n\n"
  → 前端 ReadableStream → 逐字渲染
```

### 3.3 RAG 查询 (`POST /query`)
```
用户问题: "DeepSeek V4什么时候发布？"
  │
  ▼
QueryEngine.query()
  │
  ├── [HyDE] LLM 生成假设答案 (可选)
  │     "DeepSeek V4于2025年3月由深度求索公司发布..."
  │
  ├── HybridRetriever.retrieve(假设答案)
  │     ├── 向量检索 (pgvector cosine)  → top candidates
  │     ├── BM25 关键词检索 (PG tsvector)
  │     ├── 短问题(<15字) → BM25权重↑ / 长问题 → 向量权重↑
  │     └── coarse_k=20 (短问题+10)
  │
  ├── bge-reranker-large 精排 → top_k=5
  │     ├── 最高分 ≤ 0.35 → 返回 "没有找到相关信息"
  │     └── 最高分 > 0.35 → 继续
  │
  ├── 拼 context: "[1] content1\n\n[2] content2..."
  │
  └── LLM 生成 (YAML prompt 模板)
        → {answer: "根据参考资料[1]...", sources: [...]}
```

### 3.4 文档上传 (`POST /upload`)
```
上传文件 (PDF/PPTX/DOCX)
  │
  ├── 计算 MD5
  ├── 查 md5_store.json → 命中 → 返回 duplicate
  │
  ├── 保存到 data/documents/{doc_id}.{ext}
  │
  ├── DocumentLoader.load()
  │     ├── .pdf  → 检测扫描件? → OCR/PyMuPDF
  │     ├── .pptx → python-pptx
  │     └── .docx → python-docx
  │
  ├── Chunker (1024 chars, 100 overlap)
  │
  ├── LLM 生成摘要 → 存 md5_store.json
  │
  ├── ingest_documents() → IngestionPipeline
  │     ├── SentenceSplitter → Embedding (bge) → pgvector
  │     └── metadata: {source: doc_id, filename, pages, ...}
  │
  └── 返回 {doc_id, status: "indexed", chunks_count}
```

---

## 四、关键设计决策

### 4.1 Provider 抽象
所有 LLM 遵循 `BaseLLMProvider` 接口（`chat()` + `is_available()`）。新增供应商只需继承 + 实现，在 `get_llm()` 中 register。

### 4.2 延迟导入
所有重依赖（pymupdf, paddleocr, llama_index, FlagEmbedding, redis）不在模块顶层 import，只在方法调用时加载。确保模块始终可 import。

### 4.3 单例模式
LLM Router、Reranker、Embedding、Cache 等重量级对象使用 `@lru_cache(maxsize=1)` 确保全局唯一。

### 4.4 Fail-open 策略
- LLM 失败 → 自动降级到 fallback chain
- pgvector 不可用 → 自动切 ChromaDB
- Redis 不可用 → 限流器放行，缓存返回空
- LLM 摘要失败 → 降级为文本前 200 字

### 4.5 Prompt 与代码分离
Prompt 模板存 YAML，代码通过 `prompt_loader.py` 加载并注入变量。prompt 工程师编辑 YAML 即可调优，无需改 Python。

---

## 五、数据库 Schema

```
PostgreSQL:
├── t_user (id, username, password_hash, display_name, created_at)
├── t_session_info (id, title, user_id, created_at, updated_at)
├── t_session_message (id, session_id FK→t_session_info, role, content, created_at)
└── data_documents (id, text, metadata_ JSONB, node_id, embedding VECTOR(1024),
                     text_search_tsv TSVECTOR)
    └── metadata_ 关键字段:
        ├── source: 我们的 doc_id (LlamaIndex 覆盖 doc_id 为 UUID)
        ├── filename, parser_used, pages
        └── _node_content: LlamaIndex 内部字段 (含完整元数据)
```

### 关键兼容性问题
LlamaIndex PGVectorStore 会将传入的 `doc_id` 覆盖为 UUID (`ref_doc_id`)，原始值移到 `source` 字段。因此所有查询用 `COALESCE(metadata_->>'source', metadata_->>'doc_id')` 读取。

---

## 六、前端架构

```
ai-assistant-web/
├── src/
│   ├── main.ts              # Vue 入口
│   ├── App.vue              # 布局壳
│   ├── router/index.ts      # /login, /chat, /documents
│   ├── api/index.ts         # Axios 实例 + JWT 拦截器
│   ├── stores/              # Pinia (chat.ts, document.ts)
│   └── views/
│       ├── LoginView.vue    # 登录页
│       ├── ChatView.vue     # 对话页 ★
│       └── DocumentsView.vue # 文档管理页 ★
│
└── vite.config.ts           # /api → :8000 代理
```

---

## 七、重构建议

### 已知问题

| 问题 | 严重度 | 建议 |
|------|--------|------|
| ChatView.vue 过大 (400+ 行) | 中 | 拆分 send() 和 onFile() 到 composable |
| upload.py 有重复逻辑 | 中 | 合并 SSE 和阻塞式上传的共用代码 |
| LlamaIndex 覆盖 doc_id | 高 | 调研 PGVectorStore 配置项；或迁移到自建表 |
| documents.py 混合了列表+详情 | 低 | 分离为两个文件 |
| agent/ 目录为空 | 低 | 第三期实现或删除 |
| .env 配置敏感信息在代码里 | 中 | 强制 .env.example 模板 |
| 缺少 Rate Limiting on upload | 中 | 启用 Redis 限流 |
| Summary 存 MD5 store 不是数据库 | 低 | 可改为独立表但必要性不大 |
