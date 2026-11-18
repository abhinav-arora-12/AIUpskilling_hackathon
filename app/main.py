"""FastAPI app: the public API from design.md §7.

Run: uv run uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app import config as C
from app.agent.gateway import get_gateway
from app.telemetry import recent_spans

TENANT_RE = r"^[a-z0-9_]{3,64}$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    gw = await get_gateway()
    yield
    await gw.stop()


app = FastAPI(title="The Unreliable Assistant", version="0.1.0", lifespan=lifespan)


class QueryIn(BaseModel):
    tenant_id: str = Field(pattern=TENANT_RE)
    user_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    query: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class DecisionIn(BaseModel):
    tenant_id: str = Field(pattern=TENANT_RE)
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    reviewer_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500)


def _check_tenant(header_tenant: str | None, body_tenant: str) -> None:
    if header_tenant is None or header_tenant != body_tenant:
        raise HTTPException(status_code=403, detail={"error": "E_RBAC_DENIED",
                                                      "message": "X-Tenant-ID must match tenant_id"})


async def _mcp(server: str, tool: str, args: dict) -> dict:
    gw = await get_gateway()
    res = await gw.hub.call(server, tool, args)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res)
    return res


@app.post("/api/v1/agent/query")
async def query(
    body: QueryIn,
    request: Request,
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_idempotency_key: Annotated[str | None, Header()] = None,
    traceparent: Annotated[str | None, Header()] = None,
):
    _check_tenant(x_tenant_id, body.tenant_id)
    gw = await get_gateway()
    trace_id = None
    if traceparent:  # W3C traceparent: version-traceid-parentid-flags
        m = re.match(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$", traceparent)
        trace_id = m.group(1) if m else None
    return await gw.handle(
        query=body.query,
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        session_id=body.session_id,
        idempotency_key=x_idempotency_key,
        trace_id=trace_id,
    )


@app.get("/api/v1/hitl/pending")
async def pending(tenant_id: str):
    if not re.match(TENANT_RE, tenant_id):
        raise HTTPException(status_code=400, detail={"error": "E_VALIDATION"})
    return await _mcp("state", "approval_list", {"tenant_id": tenant_id, "status": "PENDING"})


@app.post("/api/v1/hitl/{approval_id}/decision")
async def decide(approval_id: str, body: DecisionIn, x_tenant_id: Annotated[str | None, Header()] = None):
    _check_tenant(x_tenant_id, body.tenant_id)
    if not re.match(r"^[0-9a-f\-]{36}$", approval_id):
        raise HTTPException(status_code=400, detail={"error": "E_VALIDATION"})
    res = await _mcp("state", "approval_decide", {
        "tenant_id": body.tenant_id, "approval_id": approval_id,
        "reviewer_id": body.reviewer_id, "decision": body.decision, "reason": body.reason,
    })
    return res


@app.get("/api/v1/health")
async def health():
    gw = await get_gateway()
    return {"status": "ok", "mcp": {
        "hybrid_rag": gw.hub.status.get("rag", "down"),
        "state_store": gw.hub.status.get("state", "down"),
        "graphify_vault": gw.hub.status.get("graph", "down"),
    }, "breaker_open_keys": sum(1 for v in gw.breaker.failures.values() if v >= gw.breaker.max_failures),
        "cache": gw.cache.stats()}


@app.post("/api/v1/breaker/reset")
async def breaker_reset():
    gw = await get_gateway()
    gw.breaker.reset()
    return {"status": "reset", "open_keys": 0}


@app.get("/api/v1/traces")
async def traces(limit: int = 200):
    return {"spans": recent_spans(min(limit, 500))}


@app.get("/api/v1/kpi")
async def kpi():
    path = C.PATHS.results / "kpis.json"
    if not path.exists():
        return {"status": "NO_RESULTS", "message": "run scripts/scorecard.py"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    gw = await get_gateway()
    s = gw.cache.stats()
    from app.telemetry import prometheus_lines

    lines = [
        "# TYPE cache_hits_total counter", f"cache_hits_total {s['hits']}",
        "# TYPE cache_misses_total counter", f"cache_misses_total {s['misses']}",
        "# TYPE cache_entries gauge", f"cache_entries {s['entries']}",
        *prometheus_lines(),
    ]
    return "\n".join(lines) + "\n"
