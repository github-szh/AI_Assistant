"""OpenTelemetry tracing — exports traces to Arize Phoenix via OTLP."""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_tracer: trace.Tracer | None = None


def setup_tracing(service_name: str = "ai-assistant", endpoint: str | None = None) -> None:
    """Initialize OpenTelemetry and instrument the OpenAI SDK.

    Traces are exported via OTLP HTTP to the given endpoint
    (default http://localhost:6006/v1/traces, the Arize Phoenix port).

    If the endpoint is unreachable, traces are silently dropped —
    no crash, no blocking.
    """
    global _tracer
    if _tracer is not None:
        return

    otlp_endpoint = endpoint or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:6006",
    )
    traces_endpoint = f"{otlp_endpoint.rstrip('/')}/v1/traces"

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=traces_endpoint)
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    _tracer = trace.get_tracer(service_name)

    # Auto-instrument OpenAI SDK (covers Ali, DeepSeek, OpenAI providers)
    _instrument_openai()
    logger.info("OTLP tracing initialized, exporting to %s", traces_endpoint)


def _instrument_openai() -> None:
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        OpenAIInstrumentor().instrument()
        logger.debug("OpenAI SDK instrumented")
    except Exception:
        logger.debug("OpenAI instrumentor not available, skipping")


def get_tracer() -> trace.Tracer:
    """Return the application tracer for manual span creation."""
    if _tracer is None:
        return trace.get_tracer("ai-assistant")
    return _tracer
