# 项目查询优化实现文档

## 概述

本文档记录了对 AI Assistant 项目进行的三项查询优化，涵盖数据库连接、向量检索和数据缓存三个层面。每项优化均遵循"零外部依赖优先"原则，最大限度降低运维成本。

---

## 一、数据库连接池

### 改动文件

`src/api/deps.py`

### 背景

优化前，每次 API 请求都调用 `psycopg.connect()` 创建新的数据库连接，用完再 `close()` 关闭。这在低并发下没问题，但并发升高时：

- 每次连接都要 TCP 三次握手（约 1ms）+ PostgreSQL 认证（约 2ms）
- 数据库端需要为每个连接分配资源，连接数超过 `max_connections` 时拒绝服务
- 创建连接的时间在总响应时间中占比随并发升高而增大

### 实现方案

使用 `psycopg_pool.ConnectionPool` 管理连接池。核心代码：

```python
_pool: ConnectionPool | None = None

def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = (
            f"postgresql://{settings.pg_user}:{settings.pg_password}"
            f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
        )
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=2,    # 最少保持 2 个空闲连接
            max_size=10,   # 最大 10 个并发连接
            timeout=5,     # 等待连接的超时秒数
            open=False,    # 惰性打开，首次使用时才真正初始化
        )
        _pool.open()
    return _pool

def get_pg_connection():
    pool = _get_pool()
    return pool.connection().__enter__()
```

架构示意：

```text
┌──────────────┐     get_pg_connection()     ┌──────────────────────┐
│   API 请求 1  │ ──────────────────────────> │                      │
├──────────────┤                             │    ConnectionPool    │
│   API 请求 2  │ ──────────────────────────> │   (min_size=2       │
├──────────────┤                             │    max_size=10)     │
│   API 请求 3  │ ──────────────────────────> │                      │
└──────────────┘     close() 归还到池         └──────────────────────┘
```

### 关键参数说明

| 参数      | 值   | 说明                                                   |
|-----------|------|--------------------------------------------------------|
| `min_size` | 2    | 最少保持 2 个空闲连接，应对突发请求                     |
| `max_size` | 10   | 最大 10 个并发连接，防止打满数据库                       |
| `timeout`  | 5    | 获取连接的超时秒数，超时则抛出异常                      |
| `open`     | false | 惰性打开，模块导入时不连接，首次调用时才真正建立连接      |

### 技术要点

1. **全局单例**：连接池在模块首次导入时创建，使用 `global _pool` 持有单例。多次调用 `get_pg_connection()` 共享同一个池。

2. **上下文管理器提取**：在 psycopg_pool 3.x 中，`pool.connection()` 返回一个上下文管理器，调用 `__enter__()` 取出底层连接。`pool.connection()` 等效于 `pool.connection().__enter__()`。

3. **close() 的真实含义**：对从池中取出的连接调用 `.close()` 不会真正关闭 TCP 连接，而是将其**归还到池中**，供下一个请求复用。这意味着所有现有的 `conn.close()` 模式无需改动。

4. **零代码侵入**：现有的 `conn = get_pg_connection(); conn.execute(...); conn.close()` 模式无需改动，只需将 `psycopg.connect()` 替换为 `get_pg_connection()` 即可。

### 为什么选择 psycopg_pool 而非其他方案

| 方案             | 优点                           | 缺点                              |
|------------------|--------------------------------|-----------------------------------|
| psycopg_pool     | 官方维护，轻量，零配置          | 不支持异步（本项目未使用异步）     |
| SQLAlchemy 连接池 | 功能全面，支持 ORM              | 依赖重，与本项目仅使用 raw SQL 不符 |
| PgBouncer        | 独立进程，支持多应用共享         | 需要额外部署，运维成本高           |

本项目使用 raw SQL（无 ORM），psycopg_pool 是最轻量的选择。

### 参考来源

- [psycopg_pool 官方文档](https://www.psycopg.org/psycopg3/docs/api/pool.html) — ConnectionPool API
- [PostgreSQL 连接池最佳实践](https://wiki.postgresql.org/wiki/Connection_Pooling) — 为什么需要连接池
- HikariCP (Java) 的连接池设计哲学 — `min_size` / `max_size` 的设定依据

### 预期效果

- 高并发下（超过 50 QPS）响应时间降低 5 到 10 倍
- 数据库连接数稳定在 2 到 10 之间，不再随请求数线性增长

---

## 二、pgvector ivfflat 向量索引

### 改动文件

`src/knowledge/index_store.py`

### 背景

pgvector 存储的向量数据通过余弦距离（cosine distance）进行相似度检索。当 `data_documents` 表中数据量增长时，不加索引的检索需要计算查询向量与**每一条**存储向量的距离，即全表扫描。复杂度为 O(n)，其中 n 是文档总数。

### 实现方案

创建 ivfflat（Inverted File with Flat quantization）索引：

```python
def _init_pgvector_schema() -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_data_documents_embedding
        ON data_documents
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
```

### ivfflat 索引原理

ivfflat 是一种近似最近邻（ANN, Approximate Nearest Neighbor）索引。它通过**聚类**来减少搜索范围：

```text
无索引（全表扫描）:

  query -> [vec1, vec2, vec3, ..., vec10000] -> 逐个计算余弦距离 -> 取 top_k
  
  复杂度: O(n)，n=10000 时需要计算 10000 次距离

有 ivfflat 索引:

  初始化阶段: 将向量空间划分为 lists=100 个簇（通过 K-means 聚类）
  
  检索阶段:
  1. 计算 query 与 100 个质心的余弦距离
  2. 找到与 query 最近的质心
  3. 只搜索该质心所在簇内的向量（约 n/100 条）
  
  复杂度: O(lists + n/lists)，lists=100 时约 200 次距离计算
```

关键理解：ivfflat 是**近似**搜索而非精确搜索。它牺牲少量召回率来换取大幅度的速度提升。对于 RAG 场景，少量召回损失是可接受的，因为最终结果由 LLM 综合判断。

### 参数说明

| 参数               | 值                   | 说明                                                  |
|--------------------|----------------------|-------------------------------------------------------|
| `vector_cosine_ops` | 余弦相似度算子       | 与智谱 embedding-3 的 `dimensions=1024` 和余弦距离度量一致 |
| `lists`            | 100                  | ivfflat 的质心数量，推荐约 sqrt(n)，100 适合百万级以下数据 |

### lists 参数的选择策略

`lists` 值直接影响索引的性能和召回率：

- `lists` 越大，每个簇越小，搜索越快，但质心过多可能导致准确簇被遗漏
- `lists` 过小，每个簇过大，搜索退化为近似全表扫描
- 通用规则：`lists = sqrt(n)`，其中 n 是预期数据量
  - 1 万条数据：lists = 100
  - 100 万条数据：lists = 1000
  - 1 亿条数据：建议切换为 HNSW 索引

### 何时生效

- 该索引是**惰性创建**的：如果 `data_documents` 表不存在（还未上传过文档），`CREATE INDEX IF NOT EXISTS` 会静默跳过
- `_init_pgvector_schema()` 在 `PGVectorStore` 初始化时自动调用
- 索引在后台异步构建，不会阻塞查询（但首次构建时可能较慢，取决于数据量）
- 之后使用 ChromaDB 作为 fallback 时，ChromaDB 内部自动管理索引，无需手动创建

### 从 ivfflat 到 HNSW 的升级路径

pgvector 0.5.0+ 支持 HNSW（Hierarchical Navigable Small World）索引，性能更好但构建更慢、占用内存更多：

```sql
-- 当数据量超过百万级时，考虑切换为 HNSW
CREATE INDEX ON data_documents USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 200);
```

本方案第一阶段选择 ivfflat，原因是：
1. 当前数据量在万级别，ivfflat 足够
2. ivfflat 构建速度快，内存占用低
3. 不需要调参，`lists = 100` 即可工作

### 参考来源

- [pgvector 官方文档 - Indexing](https://github.com/pgvector/pgvector#indexing) — ivfflat 和 HNSW 索引说明
- [PostgreSQL ivfflat 原理](https://postgresml.org/blog/postgresml-is-ivfflat/) — 倒排文件索引的工作机制
- [向量数据库索引对比](https://jina.ai/news/vector-database-index-types/) — ivfflat vs HNSW vs PQ

### 预期效果

| 数据规模 | 无索引 | 有 ivfflat | 提速倍数 |
|----------|--------|-------------|----------|
| 1 万条   | 10ms   | < 1ms       | 10x 以上  |
| 10 万条  | 100ms  | 2-5ms       | 20-50x   |
| 100 万条 | 1s+    | 10-30ms     | 30-100x  |

---

## 三、进程内内存缓存

### 改动文件

- `src/storage/cache.py` — `MemoryCache` 类实现
- `src/knowledge/query_engine.py` — RAG 查询接入缓存

### 背景

项目中已有 Redis 缓存配置（`SessionCache`、`RateLimiter`），但 Redis 服务器不是必选组件。对于单机部署场景，一个进程内的内存缓存就足够满足需求，且不需要额外安装任何软件。

此外，RAG 查询的重复率较高（用户经常反复问类似问题），每次重复查询都需要完整走一遍检索加 LLM 生成的流程，耗时 3 到 8 秒。缓存可以大幅改善这类场景的体验。

### 实现方案

#### 3.1 MemoryCache 类

在 `cache.py` 中新增 `MemoryCache` 类，这是一个线程安全的 TTL 缓存：

```python
class MemoryCache:
    def __init__(self, default_ttl: int = 300):
        self._store: dict[str, dict] = {}   # key -> {value, expires}
        self._ttl = default_ttl              # 默认过期时间（秒）
        self._lock = _threading.Lock()       # 线程锁，保证并发安全
        # 启动后台清理线程（daemon=True，主进程退出时自动结束）
        cleaner = _threading.Thread(target=self._cleanup_loop, daemon=True)
        cleaner.start()

    def get(self, key: str):
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            if item["expires"] > _time():
                return item["value"]
            # 已过期 -> 惰性删除
            self._store.pop(key, None)
            return None

    def set(self, key: str, value, ttl: int | None = None):
        with self._lock:
            self._store[key] = {
                "value": value,
                "expires": _time() + (ttl if ttl is not None else self._ttl),
            }

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()
```

核心数据结构：

```text
self._store = {
    "rag:abc123def456:5": {                    # 键：rag:{md5(question)}:{top_k}
        "value": {"answer": "...", ...},       # 值：任意 Python 对象
        "expires": 1234567890,                 # 过期时间戳
    }
}
```

#### 3.2 三种清理机制

1. **惰性清理（Lazy Eviction）**：读取时发现已过期，直接删除并返回 None。这是最常用的清理方式，不增加额外开销。

2. **批量清理（Periodic Cleanup）**：后台守护线程每 10 分钟遍历一次，批量删除过期条目。防止长期不访问的过期键积累。

```python
def _cleanup_loop(self):
    while True:
        time.sleep(600)  # 每 10 分钟
        now = _time()
        with self._lock:
            expired = [k for k, v in self._store.items() if v["expires"] <= now]
            for k in expired:
                self._store.pop(k, None)
```

3. **内存回收**：Python 的 GC 自动回收删除后的内存。删除操作只是移除字典引用，实际内存释放由 CPython 的引用计数和垃圾回收器完成。

#### 3.3 RAG 查询接入缓存

在 `query_engine.py` 的 `query()` 方法中：

```python
class QueryEngine:
    @staticmethod
    def _cache_key(question: str, top_k: int) -> str:
        return f"rag:{hashlib.md5(question.encode()).hexdigest()}:{top_k}"

    def query(self, question: str, top_k: int = 5) -> dict:
        # 1. 检查缓存
        cache = get_memory_cache()
        key = self._cache_key(question, top_k)
        cached = cache.get(key)
        if cached is not None:
            logger.debug("RAG cache hit: '%s'", question[:60])
            return cached

        # 2. 缓存未命中，执行完整 RAG 流程
        result = self._retrieve(question, top_k)   # 检索向量库
        # ... 拼接 prompt，调用 LLM ...
        response = {"answer": answer, "sources": result["sources"]}

        # 3. 写入缓存（TTL = 300 秒）
        cache.set(key, response, ttl=300)
        return response
```

流程示意：

```text
用户提问
    |
    v
计算缓存键: MD5(question) + top_k
    |
    v
缓存命中？ ── 是 ──> 直接返回缓存结果（<1ms）
    |
    否
    v
检索向量库（pgvector/ChromaDB，2-5ms）
    |
    v
拼接提示词，调用 LLM（3-8s）
    |
    v
写入缓存，设置 TTL=300s
    |
    v
返回结果
```

#### 3.4 缓存键设计

```python
f"rag:{hashlib.md5(question.encode()).hexdigest()}:{top_k}"
```

设计要点：

- **使用 MD5**：确保键长度固定为 32 位十六进制字符串，避免原始问题中的特殊字符和超长键
- **包含 top_k**：不同检索数量应区分缓存，`rag:abc:3` 和 `rag:abc:5` 是不同键
- **中文兼容**：先编码为 UTF-8 再哈希，确保不同环境下的编码一致性
- **命名空间**：使用 `rag:` 前缀，避免和项目中其他缓存键冲突

### 与 Redis 对比

| 对比维度     | MemoryCache                     | Redis                           |
|--------------|----------------------------------|----------------------------------|
| 部署依赖     | 无                               | 需要 Redis 服务                  |
| 重启持久化   | 否，进程退出后数据丢失           | 是，可配置 RDB/AOF 持久化       |
| 多进程共享   | 否，每个进程独立                 | 是，所有进程共享同一份数据      |
| 单机速度     | 纳秒级（dict 查找）              | 毫秒级（网络 IO）                |
| 淘汰策略     | TTL 过期 + 惰性删除 + 定期清理    | 8 种策略（volatile-lru, allkeys-lru 等） |
| 数据容量     | 受限于进程内存                   | 受限于服务器内存，可配置更大    |
| 并发安全     | threading.Lock                   | 单线程模型，天然安全            |

### 适用场景

`MemoryCache` 适合以下场景：

1. **单进程部署**：没有多进程负载均衡的简单部署
2. **开发/测试环境**：不想启动 Redis，但仍需要缓存功能
3. **中小规模数据**：缓存条目在数千级别，内存占用可控
4. **可容忍重启丢失**：缓存丢了对业务没有破坏性影响

不适合的场景：

1. **多进程/多实例部署**：各进程缓存不一致
2. **需要持久化的数据**：如会话状态（已有 `SessionCache` 使用 Redis）
3. **超大缓存**：超过几 GB 可能影响 GC 性能

### 参考来源

- [Python threading.Lock 文档](https://docs.python.org/3/library/threading.html#lock-objects) — 线程锁实现并发安全
- [cachetools 库](https://github.com/tkem/cachetools) — Python TTL 缓存的标准实现（本方案为其简化版）
- [MD5 哈希用于缓存键](https://redis.io/docs/manual/patterns/caching/) — 缓存键设计的通用实践

### 预期效果

- 重复提问：检索加 LLM 生成从 3 到 8 秒降为直接返回缓存结果（小于 1ms）
- 减少智谱 API 调用次数（虽然 embedding 已独立缓存，但 RAG 回答缓存进一步减少 LLM 调用）
- 降低向量库查询压力（命中缓存时完全跳过检索步骤）

---

## 依赖安装

以上三项优化新增的 Python 依赖：

```bash
# 方案一：数据库连接池
pip install psycopg-pool

# 方案二：pgvector（已在项目中安装）
# 方案三：内存缓存（使用 Python 标准库，无需额外安装）
```

## 验证方法

```python
# 验证连接池
from src.api.deps import get_pg_connection
conn = get_pg_connection()
conn.execute("SELECT 1")   # 正常执行即表示连接池工作
conn.close()               # 归还到池，非真正关闭

# 验证向量索引
# 在 psql 或数据库中执行：
# SELECT indexname FROM pg_indexes WHERE tablename = 'data_documents';
# 应包含 idx_data_documents_embedding

# 验证内存缓存
from src.storage.cache import get_memory_cache
cache = get_memory_cache()
cache.set("test", "ok")
assert cache.get("test") == "ok"
```

## 性能对比汇总

| 优化项         | 优化前             | 优化后              | 提升幅度      |
|----------------|--------------------|----------------------|---------------|
| 数据库连接     | 每次请求新建连接  | 复用连接池（2-10）  | 5-10x         |
| 向量检索       | O(n) 全表扫描      | O(log n) ivfflat    | 10-100x       |
| 重复 RAG 查询  | 完整检索+LLM 生成  | 直接返回缓存        | 1000x+        |

## 引用的方法/技术来源索引

| 技术                | 类型             | 出处                              |
|---------------------|------------------|-----------------------------------|
| ConnectionPool      | Python 库        | psycopg_pool 3.x                  |
| ivfflat             | PostgreSQL 索引  | pgvector 扩展                     |
| vector_cosine_ops   | 距离算子         | pgvector 文档                     |
| TTL 缓存            | 设计模式         | cachetools / Redis                |
| 线程锁              | Python 标准库    | threading.Lock                    |
| 惰性删除            | 缓存策略         | Redis 的惰性过期 + 定期清理       |
| ANN (近似最近邻)    | 算法             | ivfflat / HNSW / PQ               |
| HyDE (假设文档嵌入)  | 检索增强策略     | Gao et al., 2022                  |
