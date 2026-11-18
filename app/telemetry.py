"""Span recorder with the design's span names (design.md §5, brief Gate 10).

Spans go to data/traces.jsonl and a bounded in-memory buffer (for workbench tab 6).
OTel SDK export is not wired yet (stretch S2); the span names already match the brief.
"""

from __future__ import annotations

import json
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


def new_trace_id() -> str:
    return uuid.uuid4().hex


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
        record = {"name": name, "trace_id": self.trace_id, "tenant_id": self.tenant_id,
                  "start_unix": time.time(), "attrs": attrs}
        try:
            yield record["attrs"]
        finally:
            record["duration_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            self.spans.append(record)
            _emit(record)


def _emit(record: dict) -> None:
    with _lock:
        _buffer.append(record)
        PATHS.traces.parent.mkdir(parents=True, exist_ok=True)
        with PATHS.traces.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def recent_spans(limit: int = 200) -> list[dict]:
    with _lock:
        return list(_buffer)[-limit:]
