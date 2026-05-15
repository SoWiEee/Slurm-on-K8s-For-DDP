"""OTel helpers for serve.py (Phase 7-A).

Same pattern as operator/otel.py but with service.name = "slurm-rl-scheduler".
Activated by OTEL_ENABLED=true + OTEL_EXPORTER_OTLP_ENDPOINT env vars.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

_ENABLED = os.getenv("OTEL_ENABLED", "false").lower() == "true"
_tracer = None
_propagator = None

if _ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        _resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "slurm-rl-scheduler")}
        )
        _provider = TracerProvider(resource=_resource)
        _exporter = OTLPSpanExporter(
            endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector.monitoring:4317"
            )
        )
        _provider.add_span_processor(BatchSpanProcessor(_exporter))
        trace.set_tracer_provider(_provider)
        _tracer = trace.get_tracer("slurm-rl-scheduler")
        _propagator = TraceContextTextMapPropagator()
    except ImportError:
        _ENABLED = False


def enabled() -> bool:
    return _ENABLED


def current_traceparent() -> str:
    """Return the W3C traceparent for the current span context."""
    if not _ENABLED or _propagator is None:
        return ""
    try:
        carrier: dict = {}
        _propagator.inject(carrier)
        return carrier.get("traceparent", "")
    except Exception:
        return ""


@contextmanager
def job_submit_span(
    job_id: str,
    partition: str,
    gres: str,
    requested_cpus: int,
) -> Iterator[str]:
    """Open a job_submit root span; yield the traceparent string to write
    into admin_comment so the Operator can continue the trace."""
    if not _ENABLED or _tracer is None:
        yield ""
        return

    with _tracer.start_as_current_span(
        "job_submit",
        attributes={
            "job_id": job_id,
            "partition": partition,
            "gres": gres,
            "requested_cpus": requested_cpus,
        },
    ):
        yield current_traceparent()
