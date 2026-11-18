"""mcp-hybrid-rag: MCP server exposing hybrid retrieval as a tool and chunks as resources.

Run: uv run python -m mcp_servers.rag_server   (stdio transport, design.md D4/Gate 0)
"""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app import config as C
from app.rag.retriever import get_retriever

mcp = FastMCP("mcp-hybrid-rag")


@mcp.tool()
def hybrid_retrieve(
    query: Annotated[str, Field(min_length=1, max_length=2000)],
    tenant_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,64}$")],
    top_k: Annotated[int, Field(ge=1, le=50)] = C.STAGE_TOP_K,
    k1: Annotated[float, Field(ge=0.0, le=3.0)] = C.BM25_K1,
    b: Annotated[float, Field(ge=0.0, le=1.0)] = C.BM25_B,
    rrf_k: Annotated[int, Field(ge=1, le=200)] = C.RRF_K,
    rerank_k: Annotated[int, Field(ge=1, le=10)] = C.RERANK_K,
    threshold: Annotated[float, Field(ge=0.0, le=1.0)] = C.ABSTAIN_THRESHOLD,
    use_reranker: bool = True,
) -> str:
    """Run the 3-stage hybrid retrieval for one tenant. Returns JSON with candidates and abstain flag."""
    result = get_retriever().retrieve(
        query,
        tenant_id,
        k1=k1,
        b=b,
        rrf_k=rrf_k,
        rerank_k=rerank_k,
        threshold=threshold,
        top_k=top_k,
        use_reranker=use_reranker,
    )
    # Pool ids let the evaluator compute Context Recall@50 without the full payload.
    result["pool_ids"] = [c["chunk_id"] for c in result.pop("all_candidates", [])]
    return json.dumps(result)


@mcp.resource("rag://tenants/{tenant_id}/chunks/{chunk_id}")
def chunk(tenant_id: str, chunk_id: str) -> str:
    """One chunk by id, entitlement-checked. Tenant is in the path (design.md D18).

    Chunk ids contain '#', so clients URL-encode it as %23.
    """
    for c in get_retriever().chunks:
        if c.chunk_id == chunk_id:
            if tenant_id not in c.tenants:
                # Same answer as a missing chunk, so existence is not leaked across tenants.
                break
            return json.dumps(c.__dict__)
    return json.dumps({"error": "E_NOT_FOUND", "chunk_id": chunk_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
