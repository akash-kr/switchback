"""OpenTelemetry wiring → any OTLP backend: traces and logs.

Degrades gracefully: if the OTel packages aren't installed the `span()` helper
becomes a no-op context manager and `setup_logs()` is a no-op, so the engine
still runs without a backend. Point it at an OTLP backend (Jaeger, Tempo, SigNoz) with:

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    export OTEL_SERVICE_NAME=switchback

Traces: one trace per scraped URL, one span per tier attempt. Span attributes
use the keys in `Attr` so backend dashboards stay consistent.

Logs: `setup_logs()` routes the stdlib `logging` records to an OTLP backend
(opt-in — call it from the CLI / app entry point, not from library code, so we
never hijack a host app's logging). Records emitted inside a span are
auto-correlated with that trace.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager


class Attr:
    """Canonical span-attribute keys (keep dashboards consistent)."""
    HOST = "scrape.host"
    TIER = "scrape.tier"
    OUTCOME = "scrape.outcome"      # ok | short_content | botwall | http_block |
                                    # rate_limited | timeout | connection |
                                    # http_error | error | not_applicable |
                                    # deadline_exceeded | all_failed | *_skipped
    ERROR_CLASS = "scrape.error_class"  # normalized failure class (see classify_error)
    CHALLENGE = "scrape.challenge"      # bot-wall vendor when one was served
                                        # (cloudflare / datadome / akamai / …)
    STATUS_CODE = "scrape.status_code"  # upstream HTTP status when known (403/429/…)
    MD_LEN = "scrape.md_len"
    SOURCE = "scrape.source_method"
    ERROR = "scrape.error"
    COST_USD = "scrape.cost_usd"
    LATENCY_MS = "scrape.latency_ms"   # per-tier attempt, and total on the root
    DEADLINE_S = "scrape.deadline_s"   # the per-request budget that was in force


_tracer = None
_init_lock = threading.Lock()


def _init():
    global _tracer
    if _tracer is not None:
        return _tracer
    with _init_lock:
        if _tracer is not None:
            return _tracer
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            service = os.getenv("OTEL_SERVICE_NAME", "switchback")
            provider = TracerProvider(resource=Resource.create({"service.name": service}))
            # Endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT; defaults to localhost:4317.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
            _tracer = trace.get_tracer("switchback")
        except Exception:
            _tracer = False  # tried and unavailable → no-op mode
    return _tracer


_log_provider = None


def setup_logs(level: int = logging.INFO) -> bool:
    """Route stdlib logging → an OTLP backend. Idempotent. Returns False (and
    does nothing) if OTel isn't installed/configured. Opt-in: call from an app
    entry point, not from library code."""
    global _log_provider
    if _log_provider is not None:
        return bool(_log_provider)
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        service = os.getenv("OTEL_SERVICE_NAME", "switchback")
        provider = LoggerProvider(resource=Resource.create({"service.name": service}))
        provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(provider)
        handler = LoggingHandler(level=level, logger_provider=provider)
        logging.getLogger().addHandler(handler)
        _log_provider = provider
    except Exception:
        _log_provider = False  # tried and unavailable → no-op
    return bool(_log_provider)


def flush(timeout_ms: int = 5000) -> None:
    """Force-export buffered spans and logs. Call at the end of a batch/CLI run
    so telemetry lands before the process exits (the batch processors otherwise
    flush on their own ~5s timer)."""
    if _tracer:
        try:
            from opentelemetry import trace
            provider = trace.get_tracer_provider()
            if hasattr(provider, "force_flush"):
                provider.force_flush(timeout_ms)
        except Exception:
            pass
    if _log_provider:
        try:
            _log_provider.force_flush(timeout_ms)
        except Exception:
            pass


@contextmanager
def span(name: str, **attrs):
    """Start a span. No-op if OTel isn't installed/configured.

    Yields a small object with `.set(key, value)` so tiers can attach the
    outcome/length/error once they know it.
    """
    tracer = _init()
    if not tracer:
        yield _NoopSpan()
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)
        yield _RealSpan(sp)


class _NoopSpan:
    def set(self, key, value):
        pass


class _RealSpan:
    def __init__(self, sp):
        self._sp = sp

    def set(self, key, value):
        if value is not None:
            self._sp.set_attribute(key, value)
