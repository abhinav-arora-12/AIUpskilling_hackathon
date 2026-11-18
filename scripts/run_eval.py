"""Golden-set evaluation through the real gateway (MCP servers, real models).

Writes data/results/golden_run.json. Metrics that need an LLM judge are NOT claimed as RAGAS;
they are reported as proxies with status NOT_MEASURED (design.md D12).

Usage: uv run python scripts/run_eval.py [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.gateway import Gateway  # noqa: E402
from app.agent.triage import classify_rules  # noqa: E402
from app.config import PATHS  # noqa: E402

# Labelled triage set (design.md KPI-04). Hand-labelled, synthetic.
TRIAGE_LABELED = [
    ("hello", "DIRECT_ANSWER"), ("thanks", "DIRECT_ANSWER"), ("what can you do?", "DIRECT_ANSWER"),
    ("hi there", "DIRECT_ANSWER"), ("how do you work?", "DIRECT_ANSWER"),
    ("what is the weather in paris tomorrow?", "REJECT_OOS"), ("give me a joke", "REJECT_OOS"),
    ("who will win the football match?", "REJECT_OOS"), ("recipe for lasagna please", "REJECT_OOS"),
    ("release lot 44", "TRANSACTION_EXEC"), ("Please release LOT-00044 now", "TRANSACTION_EXEC"),
    ("migrate SKU99 to the new format", "TRANSACTION_EXEC"), ("quarantine LOT-88392", "TRANSACTION_EXEC"),
    ("update the status of LOT-00012", "TRANSACTION_EXEC"), ("approve LOT-00100 for shipping", "TRANSACTION_EXEC"),
    ("What is the QA sign-off window for releasing a lot?", "HYBRID_RAG"),
    ("How many SKUs are in the catalog?", "HYBRID_RAG"), ("What is the cold storage range?", "HYBRID_RAG"),
    ("When is the shift handover?", "HYBRID_RAG"), ("What is the severity 1 escalation time?", "HYBRID_RAG"),
    ("Can a reviewer approve their own request?", "HYBRID_RAG"), ("How long do refunds take?", "HYBRID_RAG"),
    ("Who may approve Tier 3 changes?", "HYBRID_RAG"), ("What counts as personal data?", "HYBRID_RAG"),
    ("Are opened perishables returnable?", "HYBRID_RAG"),
]

# Malformed-ID repair set (design.md KPI-15). Expected canonical outputs.
REPAIR_SET = [
    ("SKU99", "SKU-099"), ("sku 7", "SKU-007"), ("SKU-5", "SKU-005"), ("sku_42", "SKU-042"),
    ("lot 44", "LOT-00044"), ("lot-1", "LOT-00001"), ("LOT 88392", "LOT-88392"), ("lot_123", "LOT-00123"),
    ("SKU 100", "SKU-100"), ("sku-9", "SKU-009"), ("lot 7", "LOT-00007"), ("LOT-00044", "LOT-00044"),
    ("lot 1000", "LOT-01000"), ("SKU99 ", "SKU-099"), ("sku 3", "SKU-003"), ("lot 2", "LOT-00002"),
    ("SKU-0", "SKU-000"), ("lot 99999", "LOT-99999"), ("sku  12", "SKU-012"), ("LOT-0044", "LOT-00044"),
]

WORD = re.compile(r"[a-z0-9]+")


def content_tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "of", "to", "and", "or", "in", "is", "for", "it", "be", "by", "at", "on", "with"}
    return {w for w in WORD.findall(text.lower()) if w not in stop}


def entities(text: str) -> set[str]:
    nums = set(re.findall(r"\d+(?:[.:]\d+)?", text))
    caps = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z]+\b", text)[1:]}
    return nums | caps


def ap_at_k(rel: list[int]) -> float:
    """Average precision over the top-k list with binary relevance (RAGAS-style context precision,
    computed without an LLM judge)."""
    hits, total = 0, 0.0
    for i, r in enumerate(rel, start=1):
        if r:
            hits += 1
            total += hits / i
    return total / hits if hits else 0.0


def ndcg_at_k(rel: list[int], n_ref: int, k: int = 4) -> float:
    dcg = sum(r / math.log2(i + 1) for i, r in enumerate(rel[:k], start=1))
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(n_ref, k) + 1))
    return dcg / ideal if ideal else 0.0


def mrr_at_k(rel: list[int], k: int = 4) -> float:
    for i, r in enumerate(rel[:k], start=1):
        if r:
            return 1 / i
    return 0.0


async def run(limit: int | None) -> dict:
    items = [json.loads(line) for line in PATHS.golden.read_text(encoding="utf-8").splitlines()]
    if limit:
        items = items[:limit]
    gw = Gateway()
    await gw.start()
    records = []
    try:
        for it in items:
            t0 = time.perf_counter()
            res = await gw.handle(query=it["question"], tenant_id=it["tenant_id"],
                                  user_id=f"eval-{it['item_id']}")
            wall = (time.perf_counter() - t0) * 1000
            ref = set(it["reference_chunk_ids"])
            r = res.get("retrieval") or {}
            pool, top4 = r.get("pool_ids", []), r.get("top4_ids", [])
            rec = {
                "item_id": it["item_id"], "stratum": it["stratum"], "tenant_id": it["tenant_id"],
                "expected": it["expected"], "status": res["status"], "route": res["route"],
                "answer": res["answer"], "citations": [c["chunk_id"] for c in res["citations"]],
                "top4_ids": top4, "pool_ids": pool, "reference_chunk_ids": sorted(ref),
                "reference_answer": it["reference_answer"], "latency_ms": res["latency_ms"],
                "wall_ms": round(wall, 1), "steps": res["loop"]["steps"],
                "timings_ms": r.get("timings_ms", {}), "verification": res.get("verification"),
                "dropped_chunks": res.get("dropped_chunks", []),
            }
            records.append(rec)
            print(f"{it['item_id']} {it['stratum']:9s} {res['status']:12s} {res['latency_ms']:8.0f} ms",
                  flush=True)
    finally:
        await gw.stop()
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(records), "records": records}
    PATHS.results.mkdir(parents=True, exist_ok=True)
    (PATHS.results / "golden_run.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def triage_accuracy() -> dict:
    hits = sum(1 for q, label in TRIAGE_LABELED if classify_rules(q).route == label)
    return {"n": len(TRIAGE_LABELED), "correct": hits, "accuracy": round(hits / len(TRIAGE_LABELED), 4)}


def repair_rate() -> dict:
    from app.agent.harness import repair_lot, repair_sku

    ok = 0
    for raw, want in REPAIR_SET:
        got = repair_sku(raw) if raw.lower().lstrip().startswith("sku") else repair_lot(raw)
        ok += got == want
    return {"n": len(REPAIR_SET), "correct": ok, "rate": round(ok / len(REPAIR_SET), 4)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    result = asyncio.run(run(args.limit))
    summary = {"count": result["count"], "triage": triage_accuracy(), "repair": repair_rate()}
    PATHS.results.mkdir(parents=True, exist_ok=True)
    (PATHS.results / "eval_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
