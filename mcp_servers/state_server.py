"""mcp-state-store: MCP server over the SQLite state store (sessions, facts, approvals, audit).

Run: uv run python -m mcp_servers.state_server   (stdio transport, design.md D4)
Errors are returned as JSON {"error": <design.md §7 code>, "message": ...}, never as raised exceptions.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.state_store import StateError, StateStore

mcp = FastMCP("mcp-state-store")

TenantId = Annotated[str, Field(pattern=r"^[a-z0-9_]{3,64}$")]
UuidKey = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]


@lru_cache(maxsize=1)
def store() -> StateStore:
    return StateStore()


def _guard(fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), default=str)
    except StateError as exc:
        return json.dumps({"error": exc.code, "message": str(exc)})


@mcp.tool()
def session_facts_get(tenant_id: TenantId, session_id: str) -> str:
    """Verified facts for one session in one tenant."""
    return _guard(lambda: {"facts": store().facts_get(tenant_id, session_id)})


@mcp.tool()
def session_fact_put(
    tenant_id: TenantId,
    session_id: str,
    key: Annotated[str, Field(min_length=1, max_length=120)],
    value: Annotated[str, Field(min_length=1, max_length=2000)],
    verified_by_chunk_id: str,
) -> str:
    """Store a fact that was verified against a chunk."""
    return _guard(
        lambda: {
            "fact_id": store().fact_put(tenant_id, session_id, key, value, verified_by_chunk_id),
        }
    )


@mcp.tool()
def approval_create(
    tenant_id: TenantId,
    requested_by: Annotated[str, Field(min_length=1, max_length=64)],
    action: str,
    args_json: Annotated[str, Field(max_length=4000)],
    idempotency_key: UuidKey,
) -> str:
    """Request a Tier 3 action. Returns PENDING; nothing is executed until a reviewer approves."""

    def run():
        args = json.loads(args_json)
        row = store().approval_create(tenant_id, requested_by, action, args, idempotency_key)
        return {"approval_id": row["approval_id"], "status": row["status"], "action": row["action"]}

    return _guard(run)


@mcp.tool()
def approval_list(tenant_id: TenantId, status: str = "PENDING") -> str:
    """Approvals for a tenant, filtered by status."""
    return _guard(lambda: {"approvals": store().approval_list(tenant_id, status)})


@mcp.tool()
def approval_decide(
    tenant_id: TenantId,
    approval_id: UuidKey,
    reviewer_id: Annotated[str, Field(min_length=1, max_length=64)],
    decision: str,
    reason: Annotated[str, Field(max_length=500)] = "",
) -> str:
    """Approve or reject a pending Tier 3 request. Reviewers only; no self-approval."""
    return _guard(lambda: store().approval_decide(tenant_id, approval_id, reviewer_id, decision, reason))


@mcp.tool()
def audit_list(tenant_id: TenantId, limit: Annotated[int, Field(ge=1, le=500)] = 100) -> str:
    """Latest audit events for a tenant, with hash-chain verification."""
    return _guard(
        lambda: {
            "events": store().audit_list(tenant_id, limit),
            "chain_valid": store().audit_verify(tenant_id),
        }
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
