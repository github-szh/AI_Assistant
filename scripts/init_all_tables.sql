-- 父块上下文表：句子窗口检索模式中存储父chunk的原始文本，
-- 子chunk命中向量检索后通过parent_id查找对应的父chunk。

CREATE TABLE public.chunk_contexts (
    parent_id text NOT NULL,
    doc_id text NOT NULL,
    content text NOT NULL,
    filename text,
    chunk_index integer,
    tenant_id integer
);


ALTER TABLE public.chunk_contexts OWNER TO postgres;

-- 向量文档表（由PGVectorStore自动管理）：存储文档块的文本、
-- 1024维向量嵌入和BM25全文检索索引，支持混合检索。

CREATE TABLE public.data_documents (
    id bigint NOT NULL,
    text character varying NOT NULL,
    metadata_ json,
    node_id character varying,
    embedding public.vector(1024),
    text_search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, (text)::text)) STORED
);


ALTER TABLE public.data_documents OWNER TO postgres;

CREATE SEQUENCE public.data_documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.data_documents_id_seq OWNER TO postgres;

ALTER SEQUENCE public.data_documents_id_seq OWNED BY public.data_documents.id;

-- 文档摘要表：存储每个文档的AI摘要及其向量嵌入，用于两级检索
-- （Level 1：先检索摘要找到最相关的文档，Level 2：再在文档内精搜）。

CREATE TABLE public.doc_summaries (
    doc_id text NOT NULL,
    summary text NOT NULL,
    embedding public.vector(1024),
    filename text,
    chunk_count integer,
    created_at timestamp without time zone DEFAULT now(),
    tenant_id integer
);


ALTER TABLE public.doc_summaries OWNER TO postgres;

-- 文档元数据表：记录上传文档的元信息（文件名、类型、大小、页数、
-- 解析器、MD5哈希、切片数量等），与data_documents中的向量数据对应。

CREATE TABLE public.t_document (
    id integer NOT NULL,
    doc_id character varying(12) NOT NULL,
    filename character varying(500),
    file_type character varying(20),
    file_size bigint,
    pages integer,
    parser_used character varying(50),
    chunks_count integer,
    summary text,
    md5_hash character varying(32),
    uploaded_at timestamp with time zone DEFAULT now(),
    user_id integer,
    tenant_id integer
);


ALTER TABLE public.t_document OWNER TO postgres;

CREATE SEQUENCE public.t_document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.t_document_id_seq OWNER TO postgres;

ALTER SEQUENCE public.t_document_id_seq OWNED BY public.t_document.id;

-- 对话会话表：存储每次对话的元信息（标题、用户、摘要、创建/更新时间），
-- 按租户和用户隔离，用于会话列表展示和对话历史管理。

CREATE TABLE public.t_session_info (
    id character varying(16) NOT NULL,
    title character varying(200) DEFAULT '新对话'::character varying,
    user_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    summary text,
    tenant_id integer
);


ALTER TABLE public.t_session_info OWNER TO postgres;

-- 对话消息表：存储用户和AI的聊天消息记录，按会话ID关联，支持分页查询和会话摘要生成。

CREATE TABLE public.t_session_message (
    id integer NOT NULL,
    session_id character varying(16) NOT NULL,
    role character varying(20) NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    summarized boolean DEFAULT false NOT NULL
);


ALTER TABLE public.t_session_message OWNER TO postgres;

CREATE SEQUENCE public.t_session_message_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.t_session_message_id_seq OWNER TO postgres;

ALTER SEQUENCE public.t_session_message_id_seq OWNED BY public.t_session_message.id;

-- 租户表：实现多租户隔离，每个租户有独立的空间，租户内的用户、文档、会话互不可见。

CREATE TABLE public.t_tenant (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    code character varying(50) NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.t_tenant OWNER TO postgres;

CREATE SEQUENCE public.t_tenant_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.t_tenant_id_seq OWNER TO postgres;

ALTER SEQUENCE public.t_tenant_id_seq OWNED BY public.t_tenant.id;

-- 用户表：存储系统用户信息，包括密码哈希、角色（viewer/editor/tenant_admin/super_admin）、
-- 所属租户等，用户隶属于租户，不同租户间数据隔离。

CREATE TABLE public.t_user (
    id integer NOT NULL,
    username character varying(100) NOT NULL,
    password_hash character varying(255) NOT NULL,
    display_name character varying(100) DEFAULT ''::character varying,
    tenant_id integer,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    role character varying(20) DEFAULT 'viewer'::character varying
);


ALTER TABLE public.t_user OWNER TO postgres;

CREATE SEQUENCE public.t_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.t_user_id_seq OWNER TO postgres;

ALTER SEQUENCE public.t_user_id_seq OWNED BY public.t_user.id;


ALTER TABLE ONLY public.data_documents ALTER COLUMN id SET DEFAULT nextval('public.data_documents_id_seq'::regclass);

ALTER TABLE ONLY public.t_document ALTER COLUMN id SET DEFAULT nextval('public.t_document_id_seq'::regclass);

ALTER TABLE ONLY public.t_session_message ALTER COLUMN id SET DEFAULT nextval('public.t_session_message_id_seq'::regclass);

ALTER TABLE ONLY public.t_tenant ALTER COLUMN id SET DEFAULT nextval('public.t_tenant_id_seq'::regclass);

ALTER TABLE ONLY public.t_user ALTER COLUMN id SET DEFAULT nextval('public.t_user_id_seq'::regclass);

ALTER TABLE ONLY public.chunk_contexts
    ADD CONSTRAINT chunk_contexts_pkey PRIMARY KEY (parent_id);

ALTER TABLE ONLY public.data_documents
    ADD CONSTRAINT data_documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.doc_summaries
    ADD CONSTRAINT doc_summaries_pkey PRIMARY KEY (doc_id);

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_doc_id_key UNIQUE (doc_id);

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_md5_hash_key UNIQUE (md5_hash);

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.t_session_info
    ADD CONSTRAINT t_session_info_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.t_session_message
    ADD CONSTRAINT t_session_message_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.t_tenant
    ADD CONSTRAINT t_tenant_code_key UNIQUE (code);

ALTER TABLE ONLY public.t_tenant
    ADD CONSTRAINT t_tenant_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.t_user
    ADD CONSTRAINT t_user_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.t_user
    ADD CONSTRAINT t_user_username_key UNIQUE (username);

CREATE INDEX documents_idx ON public.data_documents USING gin (text_search_tsv);

CREATE INDEX documents_idx_1 ON public.data_documents USING btree (((metadata_ ->> 'ref_doc_id'::text)));

CREATE INDEX idx_chunk_contexts_doc_id ON public.chunk_contexts USING btree (doc_id);

CREATE INDEX idx_chunk_contexts_tenant_id ON public.chunk_contexts USING btree (tenant_id);

CREATE INDEX idx_data_documents_embedding ON public.data_documents USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');

CREATE INDEX idx_doc_summaries_tenant_id ON public.doc_summaries USING btree (tenant_id);

CREATE INDEX idx_session_message_sid_id ON public.t_session_message USING btree (session_id, id);

ALTER TABLE ONLY public.chunk_contexts
    ADD CONSTRAINT chunk_contexts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.t_tenant(id);

ALTER TABLE ONLY public.doc_summaries
    ADD CONSTRAINT doc_summaries_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.t_tenant(id);

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.t_tenant(id);

ALTER TABLE ONLY public.t_session_info
    ADD CONSTRAINT t_session_info_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.t_tenant(id);

ALTER TABLE ONLY public.t_session_info
    ADD CONSTRAINT t_session_info_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.t_user(id);

ALTER TABLE ONLY public.t_session_message
    ADD CONSTRAINT t_session_message_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.t_session_info(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.t_user
    ADD CONSTRAINT t_user_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.t_tenant(id);

