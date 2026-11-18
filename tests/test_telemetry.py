import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.telemetry as T


@pytest.fixture()
def otel(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(T, "_otel_tracer", provider.get_tracer("test"))
    return exporter


def test_spans_keep_incoming_trace_id_and_names(otel):
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    tr = T.Trace(tid, "tenant_a")
    with tr.span("triage.classify", route="HYBRID_RAG"):
        pass
    with tr.span("mcp.rag.bm25", ms_reported_by_server=1.2):
        pass
    spans = otel.get_finished_spans()
    assert [s.name for s in spans] == ["triage.classify", "mcp.rag.bm25"]
    assert all(format(s.context.trace_id, "032x") == tid for s in spans)
    assert spans[0].attributes["ua.route"] == "HYBRID_RAG"
    assert spans[0].attributes["tenant.id"] == "tenant_a"


def test_unknown_span_name_is_rejected():
    tr = T.Trace(T.new_trace_id(), "tenant_a")
    with pytest.raises(ValueError):
        with tr.span("made.up.name"):
            pass


def test_prometheus_lines_count_spans():
    tr = T.Trace(T.new_trace_id(), "tenant_a")
    with tr.span("hook.pre"):
        pass
    lines = T.prometheus_lines()
    assert any(l.startswith('ua_span_count{span="hook_pre"}') for l in lines)
