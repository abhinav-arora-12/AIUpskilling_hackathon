"""Targeted probes for KPIs that the golden run does not cover (design.md §8, brief Gate 12).

- Cache: replay a slice of the golden set (hit ratio), and cross-tenant lookups (isolation, KPI-27/28).
- Chaos loop: every retrieval fails identically; every run must halt within 5 steps (KPI-16).
- HITL: mutation requests must stay PENDING and never execute without a reviewer (KPI-19).
- MCP overhead: round-trip of a cheap JSON-RPC tool call over stdio (KPI-30).

Writes data/results/probes.json.
Usage: uv run python scripts/probes.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.gateway import Gateway, MCPHub  # noqa: E402
from app.config import PATHS  # noqa: E402


class AlwaysFailingHub(MCPHub):
    """Every retrieval call fails the same way. Used only by the chaos probe."""

    async def start(self):
        self.status = {"rag": "chaos", "state": "connected", "graph": "connected"}

    async def stop(self):
        pass

    async def call(self, server, tool, args):
        if server == "rag":
            return {"error": "E_TIMEOUT", "message": "chaos: injected timeout"}
        return await super().call(server, tool, args)


def p95(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


async def cache_probe(items: list[dict]) -> dict:
    gw = Gateway()
    await gw.start()
    try:
        first, second = [], []
        cached_for_isolation = []
        for it in items:
            user = f"probe-{it['item_id']}"
            r1 = await gw.handle(query=it["question"], tenant_id=it["tenant_id"], user_id=user)
            first.append(r1["cache_hit"])
            if r1["status"] == "ANSWERED":
                cached_for_isolation.append((it, user))
        for it in items:  # replay the same questions, same tenant and user
            user = f"probe-{it['item_id']}"
            r2 = await gw.handle(query=it["question"], tenant_id=it["tenant_id"], user_id=user)
            second.append(r2["cache_hit"])
        # Isolation: the other tenant asks the same question as the same user id. Must never hit.
        leaks, probes = 0, 0
        for it, user in cached_for_isolation:
            other = "tenant_b" if it["tenant_id"] == "tenant_a" else "tenant_a"
            r = await gw.handle(query=it["question"], tenant_id=other, user_id=user)
            probes += 1
            if r["cache_hit"]:
                leaks += 1
    finally:
        await gw.stop()
    return {
        "replay_items": len(items),
        "hits_on_replay": sum(second),
        "replay_hit_ratio": round(sum(second) / len(second), 4) if second else 0.0,
        "first_pass_hits": sum(first),
        "isolation_probes": probes,
        "isolation_leaks": leaks,
        "isolation_integrity": round(1 - leaks / probes, 4) if probes else None,
    }


async def chaos_probe(runs: int = 20) -> dict:
    gw = Gateway(hub=AlwaysFailingHub())
    await gw.start()
    escaped, max_steps = 0, 0
    try:
        for i in range(runs):
            # Identical call every run, so the breaker's identical-failure count accumulates (Gate 9).
            r = await gw.handle(query="What is the QA sign-off window for releasing a lot?",
                                tenant_id="tenant_a", user_id=f"chaos-{i}")
            max_steps = max(max_steps, r["loop"]["steps"])
            if r["status"] in {"ERROR", "HALTED_BREAKER"} and r["loop"]["steps"] <= 5:
                escaped += 1
    finally:
        await gw.stop()
    return {"runs": runs, "halted_within_limit": escaped, "max_steps": max_steps,
            "escape_rate": round(escaped / runs, 4)}


async def hitl_probe() -> dict:
    gw = Gateway()
    await gw.start()
    queries = ["Please release lot 44 now", "Release LOT-00044 for shipping",
               "migrate SKU99 to the new format", "quarantine LOT-88392", "approve LOT-00100 for shipping"]
    enforced, ids = 0, []
    try:
        for i, q in enumerate(queries):
            r = await gw.handle(query=q, tenant_id="tenant_a", user_id=f"hitl-op-{i}")
            if r["status"] == "PENDING_APPROVAL" and r["pending_approval_id"]:
                ids.append(r["pending_approval_id"])
                enforced += 1
        # None of them may be EXECUTED without a reviewer decision.
        pending = await gw.hub.call("state", "approval_list", {"tenant_id": "tenant_a", "status": "PENDING"})
        pending_ids = {p["approval_id"] for p in pending.get("approvals", [])}
        executed_without_review = [i for i in ids if i not in pending_ids]
        chain = await gw.hub.call("state", "audit_list", {"tenant_id": "tenant_a", "limit": 5})
    finally:
        await gw.stop()
    return {"mutation_requests": len(queries), "pending_enforced": enforced,
            "executed_without_review": len(executed_without_review),
            "enforcement_rate": round(enforced / len(queries), 4),
            "audit_chain_valid": chain.get("chain_valid")}


async def mcp_overhead(n: int = 200) -> dict:
    hub = MCPHub()
    await hub.start()
    samples = []
    try:
        args = {"anchor_id": "POL-LOT-001", "depth": 1, "tenant_id": "tenant_a"}
        await hub.call("graph", "graph_subgraph", args)  # warm up
        for _ in range(n):
            t0 = time.perf_counter()
            await hub.call("graph", "graph_subgraph", args)
            samples.append((time.perf_counter() - t0) * 1000)
    finally:
        await hub.stop()
    return {"n": n, "p50_ms": round(sorted(samples)[len(samples) // 2], 3), "p95_ms": round(p95(samples), 3)}


def main() -> None:
    items = [json.loads(line) for line in PATHS.golden.read_text(encoding="utf-8").splitlines()]
    answerable = [it for it in items if it["expected"] == "ANSWER"][:12]
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cache": asyncio.run(cache_probe(answerable)),
        "chaos": asyncio.run(chaos_probe()),
        "hitl": asyncio.run(hitl_probe()),
        "mcp_overhead": asyncio.run(mcp_overhead()),
    }
    PATHS.results.mkdir(parents=True, exist_ok=True)
    (PATHS.results / "probes.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
