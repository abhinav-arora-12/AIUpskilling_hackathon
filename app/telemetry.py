"""Span recorder with the brief's span names (design.md §5, brief Gate 10).

Every span goes to three sinks:
- data/traces.jsonl and an in-memory buffer (workbench tab 6),
- OpenTelemetry SDK, exported over OTLP/gRPC when OTEL_EXPORTER_OTLP_ENDPOINT is set (D33),
- per-span-name counters and durations for the Prometheus /metrics endpoint.

The incoming W3C traceparent trace id is kept, so one trace id links the HTTP call and every span.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager

from app.config import PATHS

SPAN_NAMES = (
    "hook.pre", "cache.lookup", "triage.classify", "mcp.rag.bm25", "mcp.rag.biencoder",
    "mcp.rag.rrf", "mcp.rag.rerank", "agent.react.step", "llm.generate", "hook.post.ragas",
    "mcp.state", "mcp.graph", "verify",
)

_buffer: deque[dict] = deque(maxlen=500)
_lock = threading.Lock()
_metrics: dict[str, dict[str, float]] = {}
_otel_tracer = None


def new_trace_id() -> str:
    return uuid.uuid4().hex


def _tracer():
    """OTel tracer, created once, only when an OTLP endpoint is configured."""
    global _otel_tracer
    if _otel_tracer is not None or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return _otel_tracer
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "unreliable-assistant"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"], insecure=True)))
    trace.set_tracer_provider(provider)
    _otel_tracer = trace.get_tracer("unreliable-assistant")
    return _otel_tracer


class Trace:
    def __init__(self, trace_id: str, tenant_id: str):
        self.trace_id = trace_id
        self.tenant_id = tenant_id
        self.spans: list[dict] = []

    @contextmanager
    def span(self, name: str, **attrs):
        if name not in SPAN_NAMES:
            raise ValueError(f"unknown span name: {name}")
        t0 = time.perf_counter()
        start_ns = time.time_ns()
        record = {"name": name, "trace_id": self.trace_id, "tenant_id": self.tenant_id,
                  "start_unix": start_ns / 1e9, "attrs": attrs}
        try:
            yield record["attrs"]
        finally:
            record["duration_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            self.spans.append(record)
            _emit(record)
            _export_otel(record, start_ns)


def _export_otel(record: dict, start_ns: int) -> None:
    tracer = _tracer()
    if tracer is None:
        return
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    ctx_parent = SpanContext(trace_id=int(record["trace_id"], 16), span_id=random.getrandbits(64) or 1,
                             is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    ctx = trace.set_span_in_context(NonRecordingSpan(ctx_parent))
    span = tracer.start_span(record["name"], context=ctx, start_time=start_ns)
    span.set_attribute("tenant.id", record["tenant_id"])
    for k, v in record["attrs"].items():
        if isinstance(v, (str, int, float, bool)):
            span.set_attribute(f"ua.{k}", v)
    span.end(end_time=start_ns + int(record["duration_ms"] * 1e6))


def _emit(record: dict) -> None:
    with _lock:
        _buffer.append(record)
        m = _metrics.setdefault(record["name"], {"count": 0.0, "sum_ms": 0.0})
        m["count"] += 1
        m["sum_ms"] += record["duration_ms"]
        PATHS.traces.parent.mkdir(parents=True, exist_ok=True)
        with PATHS.traces.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def recent_spans(limit: int = 200) -> list[dict]:
    with _lock:
        return list(_buffer)[-limit:]


def prometheus_lines() -> list[str]:
    """Span counters and duration sums in Prometheus text format."""
    lines = ["# TYPE ua_span_count counter", "# TYPE ua_span_duration_ms_sum counter"]
    with _lock:
        for name, m in sorted(_metrics.items()):
            label = name.replace(".", "_")
            lines.append(f'ua_span_count{{span="{label}"}} {int(m["count"])}')
            lines.append(f'ua_span_duration_ms_sum{{span="{label}"}} {m["sum_ms"]:.3f}')
    return lines
