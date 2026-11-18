"""Agent gateway: one query from ingress to response (design.md §3 user flow, §5 interactions).

Talks to the three MCP servers over stdio (mcp-hybrid-rag, mcp-state-store, mcp-graphify-vault).
Bounded loop: states INIT -> TRIAGE -> REASONING -> TOOL_CALL -> OBSERVATION -> VERIFY -> DONE/HALTED.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import config as C
from app import llm
from app.agent import generate as G
from app.agent.generate import Draft  # noqa: F401  (type reference for readers)
from app.agent.harness import (
    CircuitBreaker,
    HarnessError,
    Tier,
    check_idempotency,
    check_tier,
    new_idempotency_key,
    repair_lot,
    repair_sku,
    validate_transaction,
)
from app.agent.subagents import ExecutionSubagent, RetrievalSubagent, VerificationSubagent
from app.agent.triage import LOT_ID, SKU_ID, TRIAGE_SYSTEM, TriageResult, classify, parse_llm_triage
from app.cache.semantic import make_cache
from app.plugins.hooks import run_pre_hooks, scan_chunks
from app.telemetry import Trace, new_trace_id

ENFORCE_TIME_BUDGETS = os.environ.get("ENFORCE_TIME_BUDGETS", "0") == "1"  # see design.md D20

MESSAGES = {
    "DIRECT_ANSWER": (
        "I answer questions about the policies in the knowledge vault, and I can submit Tier 3 changes "
        "for reviewer approval. What would you like to check?"
    ),
    "REJECT_OOS": "That request is outside the scope of this assistant. It only covers the operations policy vault.",
    "ABSTAINED": (
        "I could not find a supported answer in the policy vault for this question, so I am not answering. "
        "Please rephrase or contact the policy owner."
    ),
    "BLOCKED": "This request was blocked by a safety check. Remove personal data or instructions and try again.",
}


class MCPHub:
    """Holds one stdio session per MCP server. Servers run as child processes of this app."""

    SERVERS = {
        "rag": "mcp_servers.rag_server",
        "state": "mcp_servers.state_server",
        "graph": "mcp_servers.graphify_server",
    }

    # Bulkhead pool sizes per brief Gate 9. A slow server cannot starve the others.
    POOLS = {"rag": C.RAG_POOL, "state": C.DB_POOL, "graph": C.RAG_POOL}

    def __init__(self):
        self._stack = AsyncExitStack()
        self._bulkheads = {name: asyncio.Semaphore(size) for name, size in self.POOLS.items()}
        self.sessions: dict[str, ClientSession] = {}
        self.status: dict[str, str] = {}

    async def start(self) -> None:
        from app.manifests import verify  # refuse to start a server whose manifest does not verify

        for name in self.SERVERS:
            verify(name)
        for name, module in self.SERVERS.items():
            if name == "state" and os.environ.get("STATE_MCP_URL"):
                # State store as a separate Streamable HTTP service (design.md D21).
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(os.environ["STATE_MCP_URL"]))
                transport = "streamable-http"
            else:
                params = StdioServerParameters(command=sys.executable, args=["-m", module], cwd=str(C.ROOT))
                read, write = await self._stack.enter_async_context(stdio_client(params))
                transport = "stdio"
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            self.status[name] = f"connected ({transport})"

    async def stop(self) -> None:
        await self._stack.aclose()

    async def call(self, server: str, tool: str, args: dict) -> dict:
        async with self._bulkheads[server]:
            result = await self.sessions[server].call_tool(tool, args)
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": "E_VALIDATION", "raw": text[:300]}


@dataclass
class LoopState:
    """Bounded loop bookkeeping (brief Gate 4). Iterations are a hard limit; time budgets are soft
    unless ENFORCE_TIME_BUDGETS=1 (design.md D20)."""

    states: list[str] = field(default_factory=lambda: ["INIT"])
    steps: int = 0
    started: float = field(default_factory=time.perf_counter)
    budget_breaches: list[str] = field(default_factory=list)

    def goto(self, state: str) -> None:
        self.states.append(state)

    def step(self) -> bool:
        if self.steps >= C.MAX_LOOP_ITERATIONS:
            self.goto("HALTED")
            return False
        self.steps += 1
        return True

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def check_total(self) -> bool:
        if self.elapsed_ms() > C.BUDGET_LOOP_TOTAL_MS:
            self.budget_breaches.append(f"loop_total>{C.BUDGET_LOOP_TOTAL_MS}ms")
            return not ENFORCE_TIME_BUDGETS
        return True


def _count_llm(out: dict, res: llm.LLMResult) -> None:
    """Accumulate token usage per request (KPI-29 uses these real counts)."""
    u = out.setdefault("llm_usage", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                     "provider": res.provider, "model": res.model})
    u["calls"] += 1
    u["input_tokens"] += res.input_tokens
    u["output_tokens"] += res.output_tokens
    u["cost_usd"] = round(u["cost_usd"] + res.cost_usd, 8)
    if res.ttft_ms is not None:
        u.setdefault("ttft_ms", []).append(res.ttft_ms)


class Gateway:
    def __init__(self, hub: MCPHub | None = None, cache=None):
        self.hub = hub or MCPHub()
        self.cache = cache or make_cache()
        self._llm_bulkhead = asyncio.Semaphore(C.LLM_POOL)
        self.breaker = CircuitBreaker()
        self.turns: dict[str, list[str]] = {}  # sliding window of last 3 turns per session (Gate 5)
        self.retrieval = RetrievalSubagent(self._tool)
        self.execution = ExecutionSubagent(self._tool)
        self.verification = VerificationSubagent(self._tool)

    async def start(self) -> None:
        await self.hub.start()

    async def stop(self) -> None:
        await self.hub.stop()

    async def _tool(self, server: str, tool: str, args: dict) -> dict:
        """Every MCP tool call goes through the breaker (design.md §7, E_BREAKER_OPEN)."""
        if self.breaker.is_open(tool, args):
            return {"error": "E_BREAKER_OPEN", "message": f"{tool} failed repeatedly with identical arguments"}
        result = await self.hub.call(server, tool, args)
        if "error" in result:
            self.breaker.record_failure(tool, args)
        else:
            self.breaker.record_success(tool, args)
        return result

    async def handle(
        self,
        *,
        query: str,
        tenant_id: str,
        user_id: str,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        role: str = "operator",
        trace_id: str | None = None,
    ) -> dict:
        t_start = time.perf_counter()
        trace = Trace(trace_id or new_trace_id(), tenant_id)
        loop = LoopState()
        session_id = session_id or f"s-{user_id}"
        out: dict = {
            "tenant_id": tenant_id,
            "status": None,
            "route": None,
            "answer": None,
            "citations": [],
            "cache_hit": False,
            "trace_id": trace.trace_id,
            "pending_approval_id": None,
            "loop": {},
            "pii_findings": [],
            "dropped_chunks": [],
        }

        # 1. Pre-execution hooks (fail closed)
        with trace.span("hook.pre") as attrs:
            pre, pre_trace = run_pre_hooks(query)
            attrs["hooks"] = pre_trace
        if pre.blocked:
            return self._finish(out, "BLOCKED", "BLOCKED", MESSAGES["BLOCKED"], t_start, trace, loop,
                                reason=pre.reason)
        clean = pre.text or ""
        out["pii_findings"] = [f for h in pre_trace for f in h["findings"] if f.startswith("pii:")]

        # 2. Semantic cache (tenant + user scoped)
        from app.rag.retriever import embed_query

        with trace.span("cache.lookup") as attrs:
            qvec = embed_query(clean)
            hit = self.cache.get(tenant_id, user_id, clean, qvec)
            attrs["hit"] = hit is not None
        if hit is not None:
            out.update({"cache_hit": True, "route": hit.get("route"), "citations": hit.get("citations", [])})
            return self._finish(out, "ANSWERED", hit["route"], hit["answer"], t_start, trace, loop)

        # 3. Triage
        with trace.span("triage.classify") as attrs:
            t = classify(clean)
            if t.confidence < 0.6 and llm.provider() is not None:  # low confidence: ask the LLM
                try:
                    res = llm.chat(TRIAGE_SYSTEM, clean, model=C.OPENROUTER_TRIAGE_MODEL, max_tokens=80,
                                   temperature=0.0)
                    _count_llm(out, res)
                    refined = parse_llm_triage(res.text, clean)
                    if refined is not None:
                        t = refined
                    attrs["llm_used"] = refined is not None
                except llm.LLMError as exc:
                    attrs["llm_error"] = str(exc)[:120]
            attrs.update({"route": t.route, "risk_tier": t.risk_tier, "confidence": t.confidence,
                          "intent": t.intent})
        loop.goto("TRIAGE")
        out["route"] = t.route

        # 4. Route
        if t.route == "DIRECT_ANSWER":
            loop.goto("DONE")
            return self._finish(out, "ANSWERED", t.route, MESSAGES["DIRECT_ANSWER"], t_start, trace, loop)
        if t.route == "REJECT_OOS":
            loop.goto("DONE")
            return self._finish(out, "REJECTED_OOS", t.route, MESSAGES["REJECT_OOS"], t_start, trace, loop)
        if t.route == "TRANSACTION_EXEC":
            return await self._transaction(out, clean, t, tenant_id, user_id, idempotency_key, role,
                                           t_start, trace, loop)
        return await self._rag(out, clean, qvec, t, tenant_id, user_id, session_id, t_start, trace, loop)

    # --- routes -------------------------------------------------------------------------------
    async def _transaction(self, out, clean, t: TriageResult, tenant_id, user_id, idem, role, t_start,
                           trace, loop):
        loop.goto("REASONING")
        raw: dict = {"action": t.action}
        lot = LOT_ID.search(clean)
        sku = SKU_ID.search(clean)
        if lot:
            raw["lot_id"] = repair_lot(lot.group(0).replace(" ", "-").upper())
        if sku:
            raw["sku_id"] = repair_sku(sku.group(0).replace(" ", "-").upper())
        raw = {k: v for k, v in raw.items() if v is not None}
        try:
            check_tier(Tier.TIER_3_MUTATE, role, request_only=True)
            result = validate_transaction(raw)
            key = check_idempotency(idem) if idem else new_idempotency_key()
        except HarnessError as exc:
            loop.goto("HALTED")
            return self._finish(out, "ERROR", t.route, f"{exc.code}: {exc.message}", t_start, trace, loop,
                                reason=exc.code)
        loop.goto("TOOL_CALL")
        with trace.span("mcp.state", tool="approval_create") as attrs:
            res = await self.execution.invoke("state", "approval_create", {
                "tenant_id": tenant_id, "requested_by": user_id, "action": result.args["action"],
                "args_json": json.dumps(result.args, sort_keys=True), "idempotency_key": key,
            })
            attrs["error"] = res.get("error")
        if "error" in res:
            loop.goto("HALTED")
            return self._finish(out, "ERROR", t.route, f"{res['error']}: {res.get('message', '')}", t_start, trace,
                                loop, reason=res["error"])
        out["pending_approval_id"] = res["approval_id"]
        out["repairs"] = result.repairs
        loop.goto("DONE")
        msg = (f"Requested {result.args['action']} for {result.args.get('lot_id') or result.args.get('sku_id')}. "
               "It is pending reviewer approval and has not been executed.")
        return self._finish(out, "PENDING_APPROVAL", t.route, msg, t_start, trace, loop)

    async def _rag(self, out, clean, qvec, t: TriageResult, tenant_id, user_id, session_id, t_start, trace,
                   loop):
        # REASONING -> TOOL_CALL: retrieval through mcp-hybrid-rag
        loop.goto("REASONING")
        if not loop.step():
            return self._finish(out, "HALTED_BREAKER", t.route, "Loop limit reached.", t_start, trace, loop)
        loop.goto("TOOL_CALL")
        step_t0 = time.perf_counter()
        with trace.span("agent.react.step", step=loop.steps, tool="hybrid_retrieve") as attrs:
            args = {"query": clean, "tenant_id": tenant_id}
            res = await self.retrieval.invoke("rag", "hybrid_retrieve", args)
            attrs["error"] = res.get("error")
        step_ms = (time.perf_counter() - step_t0) * 1000
        if step_ms > C.BUDGET_LOOP_STEP_MS:  # soft budget unless ENFORCE_TIME_BUDGETS (design.md D20)
            loop.budget_breaches.append(f"step{loop.steps}>{C.BUDGET_LOOP_STEP_MS}ms")
            if ENFORCE_TIME_BUDGETS:
                loop.goto("HALTED")
                return self._finish(out, "HALTED_BREAKER", t.route, "Step time budget exceeded.", t_start,
                                    trace, loop)
        if not loop.check_total():
            loop.goto("HALTED")
            return self._finish(out, "HALTED_BREAKER", t.route, "Loop time budget exceeded.", t_start,
                                trace, loop)
        if res.get("error") == "E_BREAKER_OPEN":
            loop.goto("HALTED")
            return self._finish(out, "HALTED_BREAKER", t.route,
                                "The retrieval step failed repeatedly, so the request was halted. "
                                "Check the index and try again.", t_start, trace, loop)
        if "error" in res:
            loop.goto("HALTED")
            return self._finish(out, "ERROR", t.route, f"retrieval failed: {res['error']}", t_start, trace, loop)
        self._record_stage_spans(trace, res)

        # OBSERVATION: compress and scan retrieved chunks (R10, Gate 5)
        loop.goto("OBSERVATION")
        cands = res["candidates"]
        cands, dropped = scan_chunks(cands)
        out["dropped_chunks"] = dropped
        for c in cands:
            c["text"] = G.compress_observation(c["text"])
        out["retrieval"] = {
            "pool_ids": res.get("pool_ids", []),
            "top4_ids": [c["chunk_id"] for c in cands],
            "top_score": res["top_score"],
            "threshold": res["threshold"],
            "reranker_unavailable": res["reranker_unavailable"],
            "stage_counts": res["stage_counts"],
            "timings_ms": res["timings_ms"],
        }
        if res["abstain"] or not cands:  # note: top4 ids are reported even when abstaining
            loop.goto("DONE")
            return self._finish(out, "ABSTAINED", t.route, MESSAGES["ABSTAINED"], t_start, trace, loop)

        # REASONING: generate. LLM if configured, extractive otherwise or on any LLM failure.
        loop.goto("REASONING")
        draft = None
        with trace.span("llm.generate", generator_requested="llm" if llm.provider() else "extractive") as attrs:
            if llm.provider() is not None:
                try:
                    anchor = self._goal_anchor(clean, tenant_id, session_id)
                    async with self._llm_bulkhead:
                        draft, res = G.llm_draft(clean, cands, anchor=anchor)
                    _count_llm(out, res)
                except llm.LLMError as exc:  # FMEA: LLM error or 429 -> deterministic fallback
                    attrs["llm_error"] = str(exc)[:120]
                    draft = None
            if draft is None or not draft.answer:
                draft = G.extractive(clean, cands)
            attrs["generator"] = draft.generator

        # VERIFY (Verification Subagent, D2)
        loop.goto("VERIFY")
        with trace.span("verify") as attrs:
            passed, claims = G.verify(draft, cands)
            attrs.update({"passed": passed, "claims": len(claims)})
        if not passed:
            fallback = G.extractive(clean, cands)
            passed, claims = G.verify(fallback, cands)
            if passed:
                draft = fallback
        out["verification"] = {"passed": passed, "claims": claims, "generator": draft.generator}
        if not passed or not draft.answer:
            loop.goto("DONE")
            return self._finish(out, "ABSTAINED", t.route, MESSAGES["ABSTAINED"], t_start, trace, loop)

        out["citations"] = [
            {"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "title": c["title"], "rerank_score": c["rerank_score"]}
            for c in cands if c["chunk_id"] in draft.citations
        ]
        loop.goto("DONE")
        response = self._finish(out, "ANSWERED", t.route, draft.answer, t_start, trace, loop)

        # Store verified facts (dual memory, Gate 5) and cache the answer (tenant + user scoped)
        if draft.sentences:
            await self.verification.invoke("state", "session_fact_put", {
                "tenant_id": tenant_id, "session_id": session_id, "key": "last_verified_claim",
                "value": draft.sentences[0]["text"][:2000], "verified_by_chunk_id": draft.sentences[0]["chunk_id"],
            })
        self.cache.put(tenant_id, user_id, clean, qvec, {
            "answer": draft.answer, "route": t.route, "citations": out["citations"],
        })
        turns = self.turns.setdefault(session_id, [])
        turns.append(clean)
        del turns[:-C.SLIDING_WINDOW_TURNS]
        return response

    def _goal_anchor(self, query: str, tenant_id: str, session_id: str) -> str:
        """Immutable GoalAnchorBlock (brief Gate 5). Placed first in every generation prompt."""
        # The current query is appended after generation, so the last W-1 stored turns are the prior turns.
        recent = self.turns.get(session_id, [])[-(C.SLIDING_WINDOW_TURNS - 1):]
        lines = [
            "GoalAnchorBlock (immutable):",
            f"- original_intent: {query}",
            f"- tenant: {tenant_id}",
            "- policy_constraints: answer only from sources; cite every claim; abstain if unsupported; "
            "never request or perform state changes.",
            f"- recent_turns: {json.dumps(recent)}",
        ]
        return "\n".join(lines)

    def _record_stage_spans(self, trace: Trace, res: dict) -> None:  # noqa: D401
        timings = res.get("timings_ms", {})
        for span, key in (("mcp.rag.bm25", "bm25_ms"), ("mcp.rag.biencoder", "dense_ms"),
                          ("mcp.rag.rrf", "rrf_ms"), ("mcp.rag.rerank", "rerank_ms")):
            if key in timings:
                with trace.span(span) as attrs:
                    attrs["ms_reported_by_server"] = timings[key]

    def _finish(self, out, status, route, answer, t_start, trace: Trace, loop: LoopState, reason=None):
        out.update({
            "status": status,
            "route": route,
            "answer": answer,
            "latency_ms": round((time.perf_counter() - t_start) * 1000, 2),
            "loop": {"states": loop.states, "steps": loop.steps, "budget_breaches": loop.budget_breaches},
        })
        if reason:
            out["reason"] = reason
        with trace.span("hook.post.ragas") as attrs:
            attrs["ragas"] = "NOT_MEASURED_LIVE"  # live RAGAS needs a judge LLM; offline eval only (D12)
        return out


_gateway: Gateway | None = None


async def get_gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
        await _gateway.start()
    return _gateway
