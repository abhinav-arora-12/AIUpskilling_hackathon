"""mcp-graphify-vault: MCP server exposing depth-limited subgraphs of the vault.

Run: uv run python -m mcp_servers.graphify_server   (stdio transport, design.md D4)
"""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.graph import subgraph

mcp = FastMCP("mcp-graphify-vault")


@mcp.tool()
def graph_subgraph(
    anchor_id: Annotated[str, Field(pattern=r"^[A-Z]{2,4}-[A-Z]{2,5}-\d{3}$")],
    depth: Annotated[int, Field(ge=1, le=2)],
    tenant_id: Annotated[str, Field(pattern=r"^[a-z0-9_]{3,64}$")],
) -> str:
    """Minimal connected subgraph around one policy note, at most 2 hops (brief Gate 17)."""
    return json.dumps(subgraph(anchor_id, depth, tenant_id))


if __name__ == "__main__":
    mcp.run(transport="stdio")
