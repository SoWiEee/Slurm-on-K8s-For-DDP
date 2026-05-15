"""OpenTelemetry helpers for the Slurm Operator (Phase 7-A).

Provides a thin wrapper around the OTel SDK so the rest of the operator
can call `get_tracer()` and `extract_context(traceparent)` without
importing OTel directly.

Environment variables (set via Helm chart / operator Deployment):
  OTEL_EXPORTER_OTLP_ENDPOINT  e.g. "http://otel-collector.monitoring:4317"
  OTEL_SERVICE_NAME            defaults to "slurm-operator"
  OTEL_ENABLED                 "true" to activate (default: "false")

When OTEL_ENABLED != "true" or the SDK is not installed, all calls
become no-ops so the operator runs unchanged in environments without OTel.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

_ENABLED = os.getenv("OTEL_ENABLED", "false").lower() == "true"
_tracer = None

if _ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        _resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "slurm-operator")}
        )
        _provider = TracerProvider(resource=_resource)
        _exporter = OTLPSpanExporter(
            endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector.monitoring:4317"
            )
        )
        _provider.add_span_processor(BatchSpanProcessor(_exporter))
        trace.set_tracer_provider(_provider)
        _tracer = trace.get_tracer("slurm-operator")
        _propagator = TraceContextTextMapPropagator()
    except ImportError:
        _ENABLED = False


def get_tracer():
    return _tracer


def enabled() -> bool:
    return _ENABLED


def extract_context(traceparent: str):
    """Return an OTel Context from a W3C traceparent string, or None."""
    if not _ENABLED or not traceparent:
        return None
    try:
        from opentelemetry import context as otel_context
        carrier = {"traceparent": traceparent}
        return _propagator.extract(carrier)
    except Exception:
        return None


def inject_traceparent(span) -> str:
    """Serialize the span's trace context to a W3C traceparent string."""
    if not _ENABLED or span is None:
        return ""
    try:
        carrier: dict = {}
        from opentelemetry import context as otel_context
        _propagator.inject(carrier, context=otel_context.get_current())
        return carrier.get("traceparent", "")
    except Exception:
        return ""


@contextmanager
def start_span(
    name: str,
    parent_context=None,
    attributes: Optional[dict] = None,
    start_time_ns: Optional[int] = None,
) -> Iterator:
    """Context manager that yields an OTel span (or a no-op object).

    start_time_ns: Unix epoch in nanoseconds; use to record a span that
    covers a historical window (e.g., provisioning start → pods-ready).
    """
    if not _ENABLED or _tracer is None:
        yield _NoopSpan()
        return

    from opentelemetry import trace, context as otel_context
    token = otel_context.attach(parent_context) if parent_context else None
    kwargs = {"attributes": attributes or {}}
    if start_time_ns is not None:
        kwargs["start_time"] = start_time_ns
    with _tracer.start_as_current_span(name, **kwargs) as span:
        try:
            yield span
        finally:
            if token is not None:
                otel_context.detach(token)


class _NoopSpan:
    """Returned when OTel is disabled so callers need no guards."""
    def set_attribute(self, key, value):
        pass

    def record_exception(self, exc):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
