"""FastAPI middleware — logging, timing, Prometheus metrics, error capture."""

import time
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.monitoring.metrics import http_request_duration_seconds, http_requests_total, record_http

logger = logging.getLogger(__name__)

_SKIP_PATHS = ("/metrics",)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_SKIP_PATHS):
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            status = response.status_code
        except Exception:
            elapsed = time.perf_counter() - start
            status = 500
            raise
        finally:
            http_requests_total.labels(
                method=request.method, endpoint=request.url.path, status=str(status),
            ).inc()
            record_http(request.method, request.url.path, status)
            http_request_duration_seconds.labels(
                method=request.method, endpoint=request.url.path,
            ).observe(elapsed)

            level = logging.WARNING if status >= 400 else logging.INFO
            logger.log(
                level, "%s %s → %d (%.3fs)", request.method, request.url.path, status, elapsed,
            )
        return response
