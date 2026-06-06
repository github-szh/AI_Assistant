"""SQLite persistence for monitoring data."""

import logging
import os
import time
import sqlite3
import threading

logger = logging.getLogger(__name__)

_db_path: str | None = None
_local = threading.local()


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = os.environ.get(
            "MONITORING_DB",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "monitoring.db"),
        )
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    return _db_path


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(_get_db_path())
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


def init_db() -> None:
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost REAL NOT NULL DEFAULT 0,
            elapsed REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS http_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            method TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            status INTEGER NOT NULL,
            duration REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rag_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            query_hash TEXT NOT NULL,
            query_text TEXT NOT NULL DEFAULT '',
            max_score REAL NOT NULL DEFAULT 0,
            score_bucket TEXT NOT NULL DEFAULT 'none',
            chunk_count INTEGER NOT NULL DEFAULT 0,
            elapsed REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_http_requests_ts ON http_requests(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_queries_ts ON rag_queries(ts DESC)")
    conn.commit()
    logger.info("Monitoring DB initialized at %s", _get_db_path())


def save_llm_call(provider: str, model: str, prompt_tokens: int, completion_tokens: int, cost: float, elapsed: float) -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO llm_calls (ts, provider, model, prompt_tokens, completion_tokens, cost, elapsed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), provider, model, prompt_tokens, completion_tokens, cost, elapsed),
        )
        conn.commit()
    except Exception:
        logger.debug("Failed to save LLM call", exc_info=True)


def get_llm_calls(limit: int = 100) -> list[dict]:
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT ts, provider, model, prompt_tokens, completion_tokens, cost, elapsed FROM llm_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("Failed to read LLM calls", exc_info=True)
        return []


def get_cost_summary(days: int = 7) -> list[dict]:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            """SELECT date(ts, 'unixepoch') as day, provider, model,
                      SUM(cost) as total_cost, SUM(prompt_tokens) as total_prompt, SUM(completion_tokens) as total_completion
               FROM llm_calls WHERE ts >= ? GROUP BY day, provider, model ORDER BY day DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("Failed to read cost summary", exc_info=True)
        return []


def save_rag_query(query_hash: str, query_text: str, max_score: float, chunk_count: int, elapsed: float) -> None:
    try:
        conn = _get_conn()
        bucket = _score_bucket(max_score)
        conn.execute(
            "INSERT INTO rag_queries (ts, query_hash, query_text, max_score, score_bucket, chunk_count, elapsed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), query_hash, query_text[:200], max_score, bucket, chunk_count, elapsed),
        )
        conn.commit()
    except Exception:
        logger.debug("Failed to save RAG query", exc_info=True)


def _score_bucket(score: float) -> str:
    if score <= 0: return "none"
    if score < 0.35: return "low"
    if score < 0.6: return "medium"
    return "high"


def get_rag_query_stats(days: int = 7) -> dict:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT score_bucket, COUNT(*) as cnt FROM rag_queries WHERE ts >= ? GROUP BY score_bucket",
            (cutoff,),
        ).fetchall()
        buckets = {r["score_bucket"]: r["cnt"] for r in rows}
        avg = conn.execute(
            "SELECT AVG(max_score) as avg_score FROM rag_queries WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        return {
            "buckets": buckets,
            "avg_score": round(avg["avg_score"], 4) if avg and avg["avg_score"] else 0,
            "total": sum(buckets.values()),
        }
    except Exception:
        logger.debug("Failed to get RAG query stats", exc_info=True)
        return {"buckets": {}, "avg_score": 0, "total": 0}


def get_llm_call_count(days: int = 30) -> int:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        row = conn.execute("SELECT COUNT(*) as cnt FROM llm_calls WHERE ts >= ?", (cutoff,)).fetchone()
        return row["cnt"] if row else 0
    except Exception:
        logger.debug("Failed to count LLM calls", exc_info=True)
        return 0


def trim_old_calls(days: int = 90) -> None:
    try:
        conn = _get_conn()
        cutoff = time.time() - days * 86400
        conn.execute("DELETE FROM llm_calls WHERE ts < ?", (cutoff,))
        conn.commit()
    except Exception:
        logger.debug("Failed to trim old calls", exc_info=True)


# ── Alert persistence ──

DEFAULT_ALERT_RULES = [
    {"metric": "cpu", "operator": "gt", "threshold": 90, "label": "CPU > 90%"},
    {"metric": "mem_pct", "operator": "gt", "threshold": 90, "label": "内存 > 90%"},
    {"metric": "disk_pct", "operator": "gt", "threshold": 90, "label": "磁盘 > 90%"},
    {"metric": "db_pool_pct", "operator": "lt", "threshold": 20, "label": "DB 连接池 < 20%"},
]


def init_alerts() -> None:
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold REAL NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            rule_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            operator TEXT NOT NULL,
            threshold REAL NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events(ts DESC)")
    # Seed default rules if empty
    count = conn.execute("SELECT COUNT(*) as cnt FROM alert_rules").fetchone()["cnt"]
    if count == 0:
        for r in DEFAULT_ALERT_RULES:
            conn.execute("INSERT INTO alert_rules (metric, operator, threshold, label) VALUES (?, ?, ?, ?)",
                         (r["metric"], r["operator"], r["threshold"], r["label"]))
    conn.commit()


def get_alert_rules() -> list[dict]:
    try:
        conn = _get_conn()
        rows = conn.execute("SELECT * FROM alert_rules").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def save_alert_event(rule_id: int, label: str, metric: str, value: float, operator: str, threshold: float) -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO alert_events (ts, rule_id, label, metric, value, operator, threshold) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), rule_id, label, metric, value, operator, threshold),
        )
        conn.commit()
    except Exception:
        pass


def get_recent_alerts(limit: int = 20) -> list[dict]:
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def acknowledge_alert(alert_id: int) -> None:
    try:
        conn = _get_conn()
        conn.execute("UPDATE alert_events SET acknowledged = 1 WHERE id = ?", (alert_id,))
        conn.commit()
    except Exception:
        pass
