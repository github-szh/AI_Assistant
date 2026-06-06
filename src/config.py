"""Global configuration via Pydantic Settings.

All settings are loaded from environment variables or .env file.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"

    openai_api_key: str = ""

    # Zhipu (智谱 GLM)
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-5.1"
    zhipu_embedding_model: str = "embedding-3"
    zhipu_embedding_url: str = "https://open.bigmodel.cn/api/paas/v4/embeddings"
    embedding_dim: int = 1024
    embedding_batch_size: int = 32

    # Alibaba Cloud (DashScope)
    ali_api_key: str = ""
    ali_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ali_chat_model: str = "qwen-plus"
    ali_embedding_model: str = "text-embedding-v3"

    # Provider selection
    llm_provider: str = "deepseek"
    embedding_provider: str = "zhipu"

    # llama-parse
    llama_cloud_api_key: str = ""

    # PostgreSQL / pgvector
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "ai_assistant"
    pg_user: str = "postgres"
    pg_password: str = "changeme"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Storage (MinIO / S3)
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "ai-documents"

    # SSL
    ssl_verify: bool = True
    ssl_cert_bundle: str = ""

    # Retrieval
    retrieval_coarse_k: int = 40
    retrieval_fine_k: int = 5
    retrieval_short_query_boost: int = 10
    retrieval_short_query_len: int = 15
    retrieval_stage1_threshold: float = 0.65  # cosine similarity, triggers Stage 2 (HyDE + reranker)
    retrieval_stage2_threshold: float = 0.0  # deprecated — filtering moved into reranker
    retrieval_mode: str = "hybrid"
    rerank_min_score: float = 0.0  # raw logit threshold (BGE: >0 = relevant), applied pre-normalization
    rerank_enabled: bool = True  # toggle reranker on/off

    # Quality Guard 配置
    quality_guard_enabled: bool = True

    # Judge 模型配置（交叉评判——与生成模型不同，避免自我增强偏差）
    quality_judge_provider: str = ""   # 空字符串表示跟随 llm_provider
    quality_judge_model: str = "deepseek/deepseek-v4-flash"  # 轻量模型做评判，降低成本

    # 评估维度（safety/factuality/relevance/retrieval_quality）
    quality_eval_dimensions: list[str] = ["safety", "factuality", "relevance", "retrieval_quality"]

    # 每次 LLM Judge 调用的超时秒数
    quality_judge_timeout_s: int = 10

    # 是否并行执行各维度的评估（设为 True 以降低总延迟）
    quality_parallel_eval: bool = True

    # 安全维度采用 fail-closed 策略：Judge 异常时拦截回答
    quality_fail_closed_for_safety: bool = True

    # 非安全维度采用 fail-open 策略：Judge 异常时放行并记录警告
    quality_fail_open_for_others: bool = True

    # 送入 Judge 的最大回答字符数（防止超长上下文溢出）
    quality_max_answer_chars_for_judge: int = 4000

    # Judge 超时时是否跳过质检而非阻塞整个流程
    quality_skip_on_timeout: bool = True

    # 事实性检查提供者（llm=LLM交叉验证, vector=向量匹配零token）
    factuality_provider: str = "llm"

    # Chunking
    chunk_strategy: str = "sentence"  # fixed_size / sentence / markdown_header / recursive
    chunk_size: int = 1024
    chunk_overlap: int = 100
    sentence_window_enabled: bool = False  # parent-child chunking for sentence/recursive strategies
    sentence_window_auto_threshold: int = 20  # auto-enable when chunk count >= this value (unless explicit)

    # Two-level retrieval (document summary index)
    summary_search_top_k: int = 3   # Level 1: how many relevant documents to select
    two_stage_min_docs: int = 10    # skip Level 1 when KB has fewer docs than this

    # Chat context
    chat_max_rounds: int = 30
    chat_context_tokens: int = 8000
    chat_page_size: int = 20

    # Summarization
    chat_summarize_trigger: int = 200
    chat_summarize_keep_recent: int = 20

    # JWT
    jwt_secret: str = "change-me-in-production-must-be-32-chars!"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "DEBUG"

    # Paths
    data_dir: str = "data"
    prompts_dir: str = "prompts"
    models_cache_dir: str = "data/models"

    @property
    def pg_dsn(self) -> str:
        """Sync DSN for PG schema init (uses psycopg)."""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def pg_async_dsn(self) -> str:
        """Async DSN for PGVectorStore internal operations."""
        return (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )


# Global singleton — import from wherever needed
settings = Settings()
