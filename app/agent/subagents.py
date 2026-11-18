"""Subagents with enforced tool scopes (brief Gate 6/7, design.md D2).

Each subagent can only call the MCP servers and tools in its allow-list. A call outside the
list raises PermissionError before anything is sent, so a bug or a prompt injection cannot
make the retrieval path mutate state.
"""

from __future__ import annotations

from typing import Awaitable, Callable

Call = Callable[[str, str, dict], Awaitable[dict]]


class Subagent:
    name = "subagent"
    allowed: frozenset[tuple[str, str]] = frozenset()

    def __init__(self, call: Call):
        self._call = call

    async def invoke(self, server: str, tool: str, args: dict) -> dict:
        if (server, tool) not in self.allowed:
            raise PermissionError(f"{self.name} may not call {server}.{tool}")
        return await self._call(server, tool, args)


class RetrievalSubagent(Subagent):
    """Read-only. Query rewriting, 3-stage hybrid RAG, citation grounding (Gate 7)."""

    name = "retrieval"
    allowed = frozenset({("rag", "hybrid_retrieve"), ("graph", "graph_subgraph")})


class ExecutionSubagent(Subagent):
    """Creates Tier 3 requests only. Never decides them (HITL, D15)."""

    name = "execution"
    allowed = frozenset({("state", "approval_create")})


class VerificationSubagent(Subagent):
    """Writes verified facts and the audit trail. Vetoes ungrounded drafts (Gate 7)."""

    name = "verification"
    allowed = frozenset({("state", "session_fact_put"), ("state", "audit_append")})
