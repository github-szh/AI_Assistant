--
-- PostgreSQL database dump
--

\restrict sTAEhwwN7tuP0cE77lwMc5NjIUZq0EQ1MbOeScIIcp1OX8THkjA6qy1yQmj0u93

-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: chunk_contexts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk_contexts (
    parent_id text NOT NULL,
    doc_id text NOT NULL,
    content text NOT NULL,
    filename text,
    chunk_index integer,
    tenant_id integer
);


--
-- Name: data_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_documents (
    id bigint NOT NULL,
    text character varying NOT NULL,
    metadata_ json,
    node_id character varying,
    embedding public.vector(1024),
    text_search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, (text)::text)) STORED
);


--
-- Name: data_documents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.data_documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: data_documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.data_documents_id_seq OWNED BY public.data_documents.id;


--
-- Name: doc_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.doc_summaries (
    doc_id text NOT NULL,
    summary text NOT NULL,
    embedding public.vector(1024),
    filename text,
    chunk_count integer,
    created_at timestamp without time zone DEFAULT now(),
    tenant_id integer
);


--
-- Name: t_document; Type: TABLE; Schema: public; Owner: -
--

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
    tenant_id integer,
    chunk_strategy character varying(20)
);


--
-- Name: t_document_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.t_document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: t_document_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.t_document_id_seq OWNED BY public.t_document.id;


--
-- Name: t_session_info; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.t_session_info (
    id character varying(32) NOT NULL,
    title character varying(200) DEFAULT '新对话'::character varying NOT NULL,
    user_id integer,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    summary text,
    tenant_id integer
);


--
-- Name: t_session_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.t_session_message (
    id integer NOT NULL,
    session_id character varying(32) NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    summarized boolean DEFAULT false
);


--
-- Name: t_session_message_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.t_session_message_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: t_session_message_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.t_session_message_id_seq OWNED BY public.t_session_message.id;


--
-- Name: t_tenant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.t_tenant (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    code character varying(50) NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: t_tenant_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.t_tenant_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: t_tenant_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.t_tenant_id_seq OWNED BY public.t_tenant.id;


--
-- Name: t_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.t_user (
    id integer NOT NULL,
    username character varying(50) NOT NULL,
    password_hash character varying(255) NOT NULL,
    display_name character varying(100),
    created_at timestamp without time zone DEFAULT now(),
    tenant_id integer,
    is_active boolean DEFAULT true,
    role character varying(20) DEFAULT 'viewer'::character varying
);


--
-- Name: t_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.t_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: t_user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.t_user_id_seq OWNED BY public.t_user.id;


--
-- Name: data_documents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_documents ALTER COLUMN id SET DEFAULT nextval('public.data_documents_id_seq'::regclass);


--
-- Name: t_document id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_document ALTER COLUMN id SET DEFAULT nextval('public.t_document_id_seq'::regclass);


--
-- Name: t_session_message id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_session_message ALTER COLUMN id SET DEFAULT nextval('public.t_session_message_id_seq'::regclass);


--
-- Name: t_tenant id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_tenant ALTER COLUMN id SET DEFAULT nextval('public.t_tenant_id_seq'::regclass);


--
-- Name: t_user id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_user ALTER COLUMN id SET DEFAULT nextval('public.t_user_id_seq'::regclass);


--
-- Name: chunk_contexts chunk_contexts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk_contexts
    ADD CONSTRAINT chunk_contexts_pkey PRIMARY KEY (parent_id);


--
-- Name: data_documents data_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_documents
    ADD CONSTRAINT data_documents_pkey PRIMARY KEY (id);


--
-- Name: doc_summaries doc_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.doc_summaries
    ADD CONSTRAINT doc_summaries_pkey PRIMARY KEY (doc_id);


--
-- Name: t_document t_document_doc_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_doc_id_key UNIQUE (doc_id);


--
-- Name: t_document t_document_md5_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_md5_hash_key UNIQUE (md5_hash);


--
-- Name: t_document t_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_document
    ADD CONSTRAINT t_document_pkey PRIMARY KEY (id);


--
-- Name: t_session_info t_session_info_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_session_info
    ADD CONSTRAINT t_session_info_pkey PRIMARY KEY (id);


--
-- Name: t_session_message t_session_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_session_message
    ADD CONSTRAINT t_session_message_pkey PRIMARY KEY (id);


--
-- Name: t_tenant t_tenant_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_tenant
    ADD CONSTRAINT t_tenant_code_key UNIQUE (code);


--
-- Name: t_tenant t_tenant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_tenant
    ADD CONSTRAINT t_tenant_pkey PRIMARY KEY (id);


--
-- Name: t_user t_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_user
    ADD CONSTRAINT t_user_pkey PRIMARY KEY (id);


--
-- Name: t_user t_user_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_user
    ADD CONSTRAINT t_user_username_key UNIQUE (username);


--
-- Name: documents_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX documents_idx ON public.data_documents USING gin (text_search_tsv);


--
-- Name: documents_idx_1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX documents_idx_1 ON public.data_documents USING btree (((metadata_ ->> 'ref_doc_id'::text)));


--
-- Name: idx_chunk_contexts_doc_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunk_contexts_doc_id ON public.chunk_contexts USING btree (doc_id);


--
-- Name: idx_chunk_contexts_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunk_contexts_tenant_id ON public.chunk_contexts USING btree (tenant_id);


--
-- Name: idx_data_documents_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_data_documents_embedding ON public.data_documents USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_msg_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_msg_session ON public.t_session_message USING btree (session_id, created_at);


--
-- Name: idx_session_message_sid_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_message_sid_id ON public.t_session_message USING btree (session_id, id);


--
-- Name: t_session_info t_session_info_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_session_info
    ADD CONSTRAINT t_session_info_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.t_user(id);


--
-- Name: t_session_message t_session_message_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.t_session_message
    ADD CONSTRAINT t_session_message_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.t_session_info(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict sTAEhwwN7tuP0cE77lwMc5NjIUZq0EQ1MbOeScIIcp1OX8THkjA6qy1yQmj0u93

