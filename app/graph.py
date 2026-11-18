"""Vault link graph for mcp-graphify-vault (design.md D19).

Nodes are vault notes. Edges are [[wikilinks]]. Built from the vault directly because the
Graphify CLI is not confirmed available (CLAUDE.md §3). Subgraphs are depth-limited and tenant-filtered.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from app.config import PATHS
from app.rag.index import WIKILINK_RE, parse_note


def build_graph(vault: Path = PATHS.vault, out: Path = PATHS.graph) -> dict:
    nodes, edges = [], []
    for path in sorted(vault.glob("*.md")):
        meta, body = parse_note(path)
        nodes.append({"id": meta["id"], "domain": meta["domain"], "tenants": meta["tenants"],
                      "tokens": len(body.split())})
        for target in sorted(set(WIKILINK_RE.findall(body))):
            edges.append({"source": meta["id"], "target": target})
    graph = {"nodes": nodes, "edges": edges}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return graph


def load_graph(path: Path = PATHS.graph) -> dict:
    if not path.exists():
        return build_graph(out=path)
    return json.loads(path.read_text(encoding="utf-8"))


def subgraph(anchor_id: str, depth: int, tenant_id: str, graph: dict | None = None) -> dict:
    """Breadth-first walk up to `depth` edges (undirected), limited to tenant-visible nodes."""
    graph = graph or load_graph()
    visible = {n["id"]: n for n in graph["nodes"] if tenant_id in n["tenants"]}
    if anchor_id not in visible:
        return {"error": "E_NOT_FOUND", "anchor_id": anchor_id, "nodes": [], "edges": []}
    adj: dict[str, set[str]] = {}
    for e in graph["edges"]:
        if e["source"] in visible and e["target"] in visible:
            adj.setdefault(e["source"], set()).add(e["target"])
            adj.setdefault(e["target"], set()).add(e["source"])
    seen = {anchor_id: 0}
    queue = deque([anchor_id])
    while queue:
        node = queue.popleft()
        if seen[node] == depth:
            continue
        for nb in adj.get(node, ()):
            if nb not in seen:
                seen[nb] = seen[node] + 1
                queue.append(nb)
    keep = set(seen)
    edges = [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep]
    # Token estimate: words in the kept notes (design.md §8 Graphify pruning).
    tokens = sum(visible[n]["tokens"] for n in keep)
    return {
        "anchor_id": anchor_id,
        "depth": depth,
        "nodes": [{"id": n, "hops": seen[n]} for n in sorted(keep)],
        "edges": edges,
        "token_estimate": tokens,
    }
