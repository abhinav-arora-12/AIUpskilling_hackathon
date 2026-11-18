"""Draft 200 more golden questions with the generation model, grounded in the vault (design.md D13, D35).

Keeps the 50 hand-written items and writes data/golden/golden_250.jsonl with strata matching the brief:
factoid 63, complex 62, multihop 50, summary 38, negative 37 (25/25/20/15/15 percent of 250).
Every generated item is checked: chunk ids must exist, multihop needs two, tenant must be entitled.
Reference answers are model-drafted and are labeled as such.

Usage: uv run python scripts/expand_golden.py
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as C  # noqa: E402
from app import llm  # noqa: E402

NEW_COUNTS = {"factoid": 51, "complex": 50, "multihop": 40, "summary": 30, "negative": 29}
STRATUM_HINT = {
    "factoid": "a single fact stated in one note (a number, date, role, or threshold)",
    "complex": "a question that needs reasoning over one note's rules (conditions, exceptions, or consequences)",
    "multihop": "a question that needs facts from TWO OR MORE notes; reference_chunk_ids must list both",
    "summary": "a request to summarize one note's policy in one or two sentences",
    "negative": "a plausible operations question that the vault does NOT answer; reference_chunk_ids must be []",
}


def vault_text() -> tuple[str, dict]:
    chunks = json.loads((C.PATHS.index_dir / "chunks.json").read_text(encoding="utf-8"))
    ids = {}
    blocks = []
    for c in chunks:
        ids[c["chunk_id"]] = c
        blocks.append(f"[{c['chunk_id']}] (tenants: {', '.join(c['tenants'])})\n{c['text']}")
    return "\n\n".join(blocks), ids


def ask(stratum: str, n: int, corpus: str, seed: int) -> list[dict]:
    system = (
        "You write evaluation questions for a policy assistant. Use only the notes provided. "
        "Reply with a JSON array only, no prose. Each item: "
        '{"question": str, "reference_answer": str, "reference_chunk_ids": [str], "tenant_id": "tenant_a" or "tenant_b"}. '
        "tenant_id must be one listed in tenants for every chunk you cite. For negative items, leave "
        "reference_answer as an empty string and reference_chunk_ids as []."
    )
    user = (
        f"Write {n} questions of this type: {STRATUM_HINT[stratum]}.\n"
        f"Vary the wording and topic. Random seed for variety: {seed}.\n\nNotes:\n{corpus}"
    )
    res = llm.chat(system, user, model=C.OPENROUTER_GENERATION_MODEL, max_tokens=4000, temperature=0.8)
    text = re.sub(r"^```(json)?|```$", "", res.text.strip(), flags=re.M).strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def valid(item: dict, stratum: str, ids: dict) -> bool:
    q = (item.get("question") or "").strip()
    refs = item.get("reference_chunk_ids") or []
    tenant = item.get("tenant_id")
    if not q or tenant not in ("tenant_a", "tenant_b"):
        return False
    if stratum == "negative":
        return refs == []
    if not refs or any(r not in ids for r in refs):
        return False
    if any(tenant not in ids[r]["tenants"] for r in refs):
        return False
    if stratum == "multihop" and len(set(refs)) < 2:
        return False
    return bool(item.get("reference_answer"))


def main() -> None:
    if not C.SETTINGS.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY not set")
    corpus, ids = vault_text()
    existing = [json.loads(x) for x in C.PATHS.golden.read_text(encoding="utf-8").splitlines()]
    seen = {e["question"].lower() for e in existing}
    new, rejected = [], {}
    rng = random.Random(250)
    for stratum, target in NEW_COUNTS.items():
        got: list[dict] = []
        attempts = 0
        while len(got) < target and attempts < 12:
            attempts += 1
            for item in ask(stratum, min(15, target - len(got) + 5), corpus, rng.randint(1, 10_000)):
                if not isinstance(item, dict):
                    continue
                q = str(item.get("question", "")).strip()
                if q.lower() in seen or not valid(item, stratum, ids):
                    rejected[stratum] = rejected.get(stratum, 0) + 1
                    continue
                seen.add(q.lower())
                got.append({"stratum": stratum, "tenant_id": item["tenant_id"], "question": q,
                            "reference_answer": item.get("reference_answer", "") if stratum != "negative" else None,
                            "reference_chunk_ids": item.get("reference_chunk_ids", []),
                            "expected": "ABSTAIN" if stratum == "negative" else "ANSWER"})
                if len(got) == target:
                    break
        if len(got) < target:
            raise SystemExit(f"could not draft {target} valid {stratum} items (got {len(got)})")
        new.extend(got)
        print(stratum, len(got), flush=True)
    out = C.PATHS.golden.parent / "golden_250.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        n = 0
        for e in existing + new:
            n += 1
            fh.write(json.dumps({
                "item_id": f"G{n:03d}", "stratum": e["stratum"], "tenant_id": e["tenant_id"],
                "question": e["question"], "reference_answer": e.get("reference_answer"),
                "reference_chunk_ids": e["reference_chunk_ids"], "expected": e["expected"],
                "synthetic": True,
                "reference_source": e.get("reference_source", "hand-written" if e not in new else "model-drafted"),
            }) + "\n")
    print(json.dumps({"written": str(out), "total": n, "rejected_by_stratum": rejected}, indent=2))


if __name__ == "__main__":
    main()
