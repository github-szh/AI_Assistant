"""Prometheus metrics definitions and resource collector."""

import logging
import time

from prometheus_client import Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger(__name__)

# ── LLM usage metrics (recorded by router after each call) ──

llm_tokens_total = Counter(
    "llm_tokens_total", "LLM tokens consumed",
    ["provider", "model", "type"],
)
llm_cost_total = Counter(
    "llm_cost_total", "Estimated LLM cost in USD",
    ["provider", "model"],
)

# Approximate pricing per 1M tokens (input/output). Update as needed.
_PRICES: dict[str, tuple[float, float]] = {
    "qwen-plus":              (0.80,  2.00),
    "qwen-max":               (2.00,  6.00),
    "deepseek-v4-pro":        (2.00,  8.00),
    "deepseek-v4-flash":      (0.30,  1.20),
    "gpt-4o":                 (2.50, 10.00),
    "gpt-4o-mini":            (0.15,  0.60),
    "glm-4.7":                (0.10,  0.10),
    "glm-4v":                 (0.10,  0.10),
    # Embedding models (input only, output=0)
    "embedding-3":            (0.70,  0.00),
    "text-embedding-v3":      (0.70,  0.00),
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~2.5 chars per token for mixed Chinese/English."""
    return max(1, len(text) * 2 // 5)


def _estimate_cost(provider: str, model: str, prompt: int, completion: int) -> float:
    prices = _PRICES.get(model)
    if prices is None:
        return 0.0
    in_price, out_price = prices
    return (prompt * in_price + completion * out_price) / 1_000_000


# ── In-memory LLM call buffer (for dashboard) ──

from collections import deque
from dataclasses import dataclass, field

_MAX_CALLS = 500


@dataclass
class LLMCallRecord:
    timestamp: float
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    elapsed: float


_llm_history: deque[LLMCallRecord] = deque(maxlen=_MAX_CALLS)


def record_llm_call(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    elapsed: float,
) -> None:
    """Store one LLM call in memory buffer and SQLite for persistence."""
    cost = _estimate_cost(provider, model, prompt_tokens, completion_tokens)
    _llm_history.append(LLMCallRecord(
        timestamp=time.time(),
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        elapsed=elapsed,
    ))
    if cost > 0:
        llm_cost_total.labels(provider=provider, model=model).inc(cost)
    # Persist to SQLite (non-blocking import to avoid circular import at module level)
    try:
        from src.monitoring.storage import save_llm_call
        save_llm_call(provider, model, prompt_tokens, completion_tokens, cost, elapsed)
    except Exception:
        pass


def get_llm_history() -> list[LLMCallRecord]:
    """Return a snapshot of recent LLM calls (newest first)."""
    return list(reversed(_llm_history))


def clear_llm_history() -> None:
    _llm_history.clear()

# HTTP request metrics (recorded by middleware)
http_requests_total = Counter(
    "http_requests_total", "Total HTTP requests",
    ["method", "endpoint", "status"],
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request duration",
    ["method", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Simple in-memory HTTP count dict (used by dashboard instead of Prometheus counters
# to avoid value corruption from module reloads)
from collections import defaultdict
_http_counts: dict[str, int] = defaultdict(int)


def record_http(method: str, endpoint: str, status: int) -> None:
    key = f"{method} {endpoint}:{status}"
    _http_counts[key] += 1


def get_http_counts() -> dict[str, int]:
    return dict(_http_counts)


# ── Latency percentile calculation ──

_HISTOGRAM_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def get_latency_percentiles() -> dict[str, float]:
    """Compute p50/p95/p99 from the Prometheus Histogram buckets."""
    try:
        total = 0
        bucket_counts: list[tuple[float, int]] = []
        for sample in http_request_duration_seconds.collect():
            for s in sample.samples:
                if s.name.endswith("_bucket"):
                    le = float(s.labels.get("le", "0"))
                    bucket_counts.append((le, int(s.value)))
                    total += int(s.value)

        if total == 0:
            return {"p50": 0, "p95": 0, "p99": 0}

        bucket_counts.sort(key=lambda x: x[0])
        targets = {
            "p50": total * 0.50,
            "p95": total * 0.95,
            "p99": total * 0.99,
        }
        # Filter out +Inf bucket for percentile calculation
        finite = [(le, c) for le, c in bucket_counts if le < float("inf")]
        if not finite:
            return {"p50": 0, "p95": 0, "p99": 0}
        result = {}
        for name, target in targets.items():
            cumulative = 0
            for le, count in finite:
                cumulative += count
                if cumulative >= target:
                    result[name] = le
                    break
            else:
                result[name] = finite[-1][0]
        return result
    except Exception:
        return {"p50": 0, "p95": 0, "p99": 0}

# System resource gauges (set by background collector)
system_cpu_percent = Gauge("system_cpu_utilization_percent", "CPU utilization")
system_memory_used_bytes = Gauge("system_memory_used_bytes", "Memory used")
system_memory_total_bytes = Gauge("system_memory_total_bytes", "Memory total")
system_disk_used_bytes = Gauge("system_disk_used_bytes", "Disk used", ["mount"])
system_disk_total_bytes = Gauge("system_disk_total_bytes", "Disk total", ["mount"])

# DB pool gauges
db_pool_min = Gauge("db_pool_min_size", "DB pool min connections")
db_pool_max = Gauge("db_pool_max_size", "DB pool max connections")
db_pool_available = Gauge("db_pool_available_connections", "DB pool available connections")


# ── GPU metrics (via pynvml, optional) ──

_gpu_metrics: dict[str, float] = {}


def collect_gpu():
    """Collect GPU metrics via pynvml (graceful if not available)."""
    global _gpu_metrics
    try:
        import pynvml
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        metrics = {}
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle).decode() if isinstance(pynvml.nvmlDeviceGetName(handle), bytes) else pynvml.nvmlDeviceGetName(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            metrics[f"gpu_{i}_name"] = name
            metrics[f"gpu_{i}_util"] = util.gpu
            metrics[f"gpu_{i}_mem_used"] = mem_info.used
            metrics[f"gpu_{i}_mem_total"] = mem_info.total
            metrics[f"gpu_{i}_temp"] = temp
        _gpu_metrics = metrics
    except Exception:
        _gpu_metrics = {}


def get_gpu_metrics() -> dict[str, float]:
    return dict(_gpu_metrics)


# ── Redis metrics ──

_redis_metrics: dict[str, float] = {}
_redis_client_ref = None


def collect_redis():
    """Collect Redis server metrics via direct connection from settings."""
    global _redis_metrics, _redis_client_ref
    try:
        if _redis_client_ref is None:
            import redis as _r
            _redis_client_ref = _r.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            _redis_client_ref.ping()
        info = _redis_client_ref.info()
        _redis_metrics = {
            "used_memory": info.get("used_memory", 0),
            "total_system_memory": info.get("total_system_memory", 0),
            "connected_clients": info.get("connected_clients", 0),
            "evicted_keys": info.get("evicted_keys", 0),
            "keyspace_hitrate": _calc_hitrate(info),
        }
    except Exception:
        _redis_metrics = {}


def _calc_hitrate(info: dict) -> float:
    hits = info.get("keyspace_hits", 0)
    misses = info.get("keyspace_misses", 0)
    total = hits + misses
    return round(hits / total, 4) if total > 0 else 0.0


def get_redis_metrics() -> dict[str, float]:
    return dict(_redis_metrics)


def collect_resources():
    """Collect system resource metrics (CPU, memory, disk)."""
    try:
        import psutil
        system_cpu_percent.set(psutil.cpu_percent(interval=0.1))
        mem = psutil.virtual_memory()
        system_memory_used_bytes.set(mem.used)
        system_memory_total_bytes.set(mem.total)
        for part in psutil.disk_partitions():
            if part.fstype:
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    system_disk_used_bytes.labels(mount=part.mountpoint).set(usage.used)
                    system_disk_total_bytes.labels(mount=part.mountpoint).set(usage.total)
                except PermissionError:
                    continue
    except Exception:
        logger.debug("Resource collection skipped (psutil not available)")


def collect_db_pool():
    """Collect DB pool stats if available."""
    try:
        from src.api.deps import pool_stats
        stats = pool_stats()
        db_pool_min.set(stats.get("min", 0))
        db_pool_max.set(stats.get("max", 0))
        db_pool_available.set(stats.get("free", -1))
    except Exception:
        logger.debug("DB pool collection skipped")
