"""POST /upload — upload and parse documents, store metadata in t_document table."""

import hashlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Query, UploadFile, File, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from src.api.deps import get_document_loader, get_pg_connection
from src.api.schemas import UploadResponse
from src.api.permissions import require_permission
from src.config import settings
from src.knowledge.ingestion import ingest_documents
from src.parsing.chunker import Chunker
from src.parsing.loader import DocumentLoader

router = APIRouter(prefix="/upload", tags=["upload"])
logger = logging.getLogger(__name__)


class CheckRequest(BaseModel):
    filename: str
    file_size: int


@router.post("/check")
async def check_duplicate(req: CheckRequest, user: dict = Depends(require_permission("document:upload"))):
    """Check if a document with the same name and size already exists."""
    # 权限与多租户：按租户检查重复
    tenant_id = user.get("tenant_id")
    conn = get_pg_connection()
    try:
        row = conn.execute(
            "SELECT doc_id, filename FROM t_document WHERE filename = %s AND file_size = %s AND tenant_id = %s",
            [req.filename, req.file_size, tenant_id],
        ).fetchone()
        if row:
            return {"exists": True, "doc_id": row[0], "filename": row[1]}
        return {"exists": False, "doc_id": None, "filename": None}
    finally:
        conn.close()


@router.post("", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    loader: DocumentLoader = Depends(get_document_loader),
    user: dict = Depends(require_permission("document:upload")),
    strategy: str | None = Query(None, description="切片策略: fixed_size / sentence / markdown_header / recursive"),
    sentence_window: bool = Query(False, description="启用句子窗口检索（父子chunk）"),
):
    # 权限与多租户：获取当前用户租户ID
    tenant_id = user.get("tenant_id")

    content = await file.read()
    file_hash = hashlib.md5(content).hexdigest()
    file_size = len(content)
    raw_name = file.filename or "unknown"
    try:
        raw_name.encode("utf-8")
    except UnicodeEncodeError:
        try:
            raw_name = raw_name.encode("latin-1").decode("gbk")
        except Exception:
            pass
    filename = raw_name
    ext = Path(filename).suffix

    # MD5 dedup — within tenant scope
    conn = get_pg_connection()
    try:
        existing = conn.execute(
            "SELECT doc_id, filename FROM t_document WHERE md5_hash=%s AND tenant_id=%s",
            [file_hash, tenant_id],
        ).fetchone()
    finally:
        conn.close()

    if existing:
        return UploadResponse(
            doc_id=existing[0],
            filename=filename,
            file_type=ext,
            status="duplicate",
            parser_used="skipped",
            chunks_count=None,
            message=f"文件已存在 (doc_id: {existing[0]})",
        )

    # Persist file to disk
    doc_id = uuid.uuid4().hex[:12]
    upload_dir = Path(settings.data_dir) / "documents"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{doc_id}{ext}"
    saved_path.write_bytes(content)

    # Parse
    parsed = []
    chunks = []
    parser_used = "unknown"
    page_count = 0
    summary = ""
    parse_error = None

    try:
        parsed = loader.load(str(saved_path))
    except Exception as exc:
        logger.exception("Failed to parse %s", filename)
        parse_error = str(exc)

    if parsed:
        parser_used = parsed[0].parser_used
        page_count = len(parsed)

        use_sw = sentence_window
        if use_sw is None or use_sw is False:
            test_chunker = Chunker(
                strategy=strategy or settings.chunk_strategy,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                sentence_window=False,
            )
            test_chunks = test_chunker.chunk(parsed)
            if len(test_chunks) >= settings.sentence_window_auto_threshold:
                use_sw = True
                logger.info("Auto-enabled Sentence Window: %d chunks >= threshold %d",
                            len(test_chunks), settings.sentence_window_auto_threshold)

        chunker = Chunker(
            strategy=strategy or settings.chunk_strategy,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            sentence_window=use_sw or False,
        )

        # Generate AI summary
        try:
            full_text = " ".join(p.content for p in parsed)[:8000]
            from src.llm.router import get_llm
            llm = get_llm()
            summary = llm.chat(
                messages=[{"role": "user", "content": (
                    "请用5-8句话总结以下文档的主要内容，涵盖文档涉及的主题、关键信息和结论。用中文回答：\n\n" + full_text
                )}],
                temperature=0.0, max_tokens=500,
            )
        except Exception:
            logger.warning("AI summary generation failed", exc_info=True)
        if not summary:
            summary = " ".join(p.content for p in parsed)[:200]

        # 权限与多租户：索引文档摘要时传入 tenant_id
        if summary:
            try:
                from src.knowledge.embeddings import get_embedding_manager
                embed_mgr = get_embedding_manager()
                summary_emb = embed_mgr.encode_query(summary)
                from src.knowledge.index_store import _ensure_summary_collection, _insert_summary
                _ensure_summary_collection()
                _insert_summary(doc_id, summary, summary_emb, filename, len(chunks), tenant_id=tenant_id)
            except Exception:
                logger.warning("Failed to index document summary", exc_info=True)

        # 权限与多租户：入库时传入 tenant_id
        try:
            if chunker.sentence_window:
                result = chunker.chunk_with_windows(parsed)
                chunks = result.index_chunks
                ingest_documents(
                    result.index_chunks,
                    doc_id=doc_id, filename=filename,
                    extra_metadata={"pages": str(page_count), "tenant_id": str(tenant_id)},
                    parent_docs=result.context_chunks,
                    child_to_parent=result.index_to_parent,
                )
            else:
                chunks = chunker.chunk(parsed)
                ingest_documents(
                    chunks,
                    doc_id=doc_id, filename=filename,
                    extra_metadata={"pages": str(page_count), "tenant_id": str(tenant_id)},
                )
        except Exception as exc:
            logger.warning("Ingestion skipped (vector store unavailable): %s", exc)

    # 权限与多租户：保存文档元数据时写入 tenant_id
    conn2 = get_pg_connection()
    try:
        conn2.execute(
            """INSERT INTO t_document
               (doc_id, filename, file_type, file_size, pages, parser_used,
                chunks_count, summary, md5_hash, user_id, tenant_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [doc_id, filename, ext, file_size, page_count, parser_used,
             len(chunks), summary, file_hash, user["user_id"], tenant_id],
        )
        conn2.commit()
    finally:
        conn2.close()

    if parse_error:
        status = "parse_failed"
        msg = f"解析失败: {parse_error}"
    elif not parsed:
        status = "no_text"
        msg = "图片中未检测到可识别文字" if ext.lower() in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp") else "文档中未提取到文字内容"
    else:
        status = "indexed" if chunks else "parsed"
        ingest_msg = f", {len(chunks)} chunks created"
        msg = f"Parsed with {parser_used}, {len(chunks)} chunks created{ingest_msg}"

    return UploadResponse(
        doc_id=doc_id,
        filename=filename,
        file_type=ext,
        status=status,
        parser_used=parser_used,
        chunks_count=len(chunks),
        message=msg,
    )


@router.post("/stream")
async def upload_stream(
    file: UploadFile = File(...),
    replace_doc_id: str | None = Query(None),
    user: dict = Depends(require_permission("document:upload")),
    strategy: str | None = Query(None, description="切片策略: fixed_size / sentence / markdown_header / recursive"),
    sentence_window: bool = Query(False, description="启用句子窗口检索（父子chunk）"),
):
    """SSE streaming upload — reports parsing progress in real-time.

    Set replace_doc_id to replace an existing document before re-ingesting.
    """
    # 权限与多租户：获取当前用户租户ID
    tenant_id = user.get("tenant_id")

    content = await file.read()
    file_hash = hashlib.md5(content).hexdigest()
    file_size = len(content)
    raw_name = file.filename or "unknown"
    try:
        raw_name.encode("utf-8")
    except UnicodeEncodeError:
        try:
            raw_name = raw_name.encode("latin-1").decode("gbk")
        except Exception:
            pass
    filename = raw_name
    ext = Path(filename).suffix

    async def generate():
        import json as _json
        yield f"data: {_json.dumps({'step':'read','msg':'读取文件中...'})}\n\n"

        # Replace: delete old data first (within tenant scope)
        if replace_doc_id:
            _delete_document(replace_doc_id, tenant_id)  # 权限与多租户：传入 tenant_id
            yield f"data: {_json.dumps({'step':'read','msg':'已删除旧文档，重新解析中...'})}\n\n"

        # Dedup (within tenant scope)
        if not replace_doc_id:
            conn = get_pg_connection()
            try:
                existing = conn.execute(
                    "SELECT doc_id FROM t_document WHERE md5_hash=%s AND tenant_id=%s",
                    [file_hash, tenant_id],
                ).fetchone()
            finally:
                conn.close()
            if existing:
                yield f"data: {_json.dumps({'step':'done','msg':'文件已存在，跳过','status':'duplicate'})}\n\n"
                return

        # Save to disk
        doc_id = uuid.uuid4().hex[:12]
        upload_dir = Path(settings.data_dir) / "documents"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_path = upload_dir / f"{doc_id}{ext}"
        saved_path.write_bytes(content)

        yield f"data: {_json.dumps({'step':'parse','msg':'正在解析文档...'})}\n\n"

        parsed = []
        chunks = []
        parser_used = "unknown"
        page_count = 0
        summary = ""
        parse_error = None

        try:
            loader = DocumentLoader()
            parsed = loader.load(str(saved_path))
        except Exception as exc:
            logger.exception("Failed to parse %s via streaming upload", filename)
            parse_error = str(exc)

        if parsed:
            yield f"data: {_json.dumps({'step':'chunk','msg':f'解析完成，正在分块...','pages':len(parsed)})}\n\n"
            parser_used = parsed[0].parser_used
            page_count = len(parsed)
            
            # Auto-detect Sentence Window
            use_sw = sentence_window
            if use_sw is None or use_sw is False:
                test_chunker = Chunker(
                    strategy=strategy or settings.chunk_strategy,
                    chunk_size=settings.chunk_size,
                    chunk_overlap=settings.chunk_overlap,
                    sentence_window=False,
                )
                test_chunks = test_chunker.chunk(parsed)
                if len(test_chunks) >= settings.sentence_window_auto_threshold:
                    use_sw = True

            chunker = Chunker(
                strategy=strategy or settings.chunk_strategy,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                sentence_window=use_sw or False,
            )

            # Generate AI summary
            summary = ""
            try:
                full_text = " ".join(p.content for p in parsed)[:8000]
                from src.llm.router import get_llm
                llm = get_llm()
                summary = llm.chat(
                    messages=[{"role": "user", "content": (
                        "请用5-8句话总结以下文档的主要内容，涵盖文档涉及的主题、关键信息和结论。用中文回答：\n\n" + full_text
                    )}],
                    temperature=0.0, max_tokens=500,
                )
                yield f"data: {_json.dumps({'step':'summary','msg':'AI摘要生成完成'})}\n\n"
            except Exception:
                logger.warning("AI summary generation failed", exc_info=True)
            if not summary:
                summary = " ".join(p.content for p in parsed)[:200]

            # 权限与多租户：ingest 时传入 tenant_id 到 metadata
            extra_meta = {"pages": str(page_count), "tenant_id": str(tenant_id)}

            if chunker.sentence_window:
                result = chunker.chunk_with_windows(parsed)
                chunks = result.index_chunks
                yield f"data: {_json.dumps({'step':'ingest','msg':f'分块完成({len(chunks)}子块 + {len(result.context_chunks)}父块)，正在写入向量库...'})}\n\n"
                try:
                    ingest_documents(
                        result.index_chunks,
                        doc_id=doc_id, filename=filename,
                        extra_metadata=extra_meta,
                        parent_docs=result.context_chunks,
                        child_to_parent=result.index_to_parent,
                    )
                except Exception as exc:
                    logger.warning("Ingestion skipped: %s", exc)
            else:
                chunks = chunker.chunk(parsed)
                yield f"data: {_json.dumps({'step':'ingest','msg':f'分块完成({len(chunks)}块)，正在写入向量库...'})}\n\n"
                try:
                    ingest_documents(chunks, doc_id=doc_id, filename=filename, extra_metadata=extra_meta)
                except Exception as exc:
                    logger.warning("Ingestion skipped: %s", exc)

        # Index document summary with tenant_id
        if summary:
            try:
                from src.knowledge.embeddings import get_embedding_manager
                embed_mgr = get_embedding_manager()
                summary_emb = embed_mgr.encode_query(summary)
                from src.knowledge.index_store import _ensure_summary_collection, _insert_summary
                _ensure_summary_collection()
                _insert_summary(doc_id, summary, summary_emb, filename, len(chunks) if chunks else 0, tenant_id=tenant_id)
            except Exception:
                logger.warning("Failed to index document summary", exc_info=True)

        # Save metadata to t_document
        conn = get_pg_connection()
        try:
            conn.execute(
                """INSERT INTO t_document
                   (doc_id, filename, file_type, file_size, pages, parser_used,
                    chunks_count, summary, md5_hash, user_id, tenant_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [doc_id, filename, ext, file_size, page_count, parser_used,
                 len(chunks) if chunks else 0, summary, file_hash, user["user_id"], tenant_id],
            )
            conn.commit()
        finally:
            conn.close()

        if parse_error:
            yield f"data: {_json.dumps({'step':'done','msg':f'解析失败: {parse_error}','doc_id':doc_id,'status':'parse_failed'})}\n\n"
        elif not parsed:
            hint = "图片中未检测到可识别文字" if ext.lower() in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp") else "文档中未提取到文字内容"
            yield f"data: {_json.dumps({'step':'done','msg':hint,'doc_id':doc_id,'status':'no_text'})}\n\n"
        else:
            n_ingested = len(chunks)
            yield f"data: {_json.dumps({'step':'done','msg':f'入库完成({n_ingested}条)','doc_id':doc_id,'chunks':len(chunks),'parser':parser_used,'status':'done'})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _delete_document(doc_id: str, tenant_id: int | None = None) -> None:
    """Remove a document from vector store, metadata table, and local filesystem."""
    # 权限与多租户：按租户删除
    conn = get_pg_connection()
    try:
        conn.execute(
            """DELETE FROM data_documents
               WHERE COALESCE(metadata_->>'source', metadata_->>'doc_id') = %s
               AND metadata_->>'tenant_id' = %s""",
            [doc_id, str(tenant_id)],
        )
        conn.execute(
            "DELETE FROM t_document WHERE doc_id = %s AND tenant_id = %s",
            [doc_id, tenant_id],
        )
        conn.execute(
            "DELETE FROM doc_summaries WHERE doc_id = %s AND tenant_id = %s",
            [doc_id, tenant_id],
        )
        conn.execute(
            "DELETE FROM chunk_contexts WHERE doc_id = %s AND tenant_id = %s",
            [doc_id, tenant_id],
        )
        conn.commit()
    finally:
        conn.close()

    docs_dir = Path(settings.data_dir) / "documents"
    if docs_dir.exists():
        for f in docs_dir.iterdir():
            if f.stem == doc_id:
                f.unlink()
                logger.info("Deleted local file: %s", f.name)
                break
