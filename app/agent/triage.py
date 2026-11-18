"""Triage: classify (intent, domain, risk_tier, complexity) and route (design.md §3, D8, D9).

Rules run first, locally, inside the 25 ms budget. The LLM is used only for low-confidence cases,
and only when an API key is configured. Without a key, low-confidence cases use the safe default
HYBRID_RAG, which is then grounded or abstained by retrieval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.plugins.hooks import normalize

ROUTES = ("DIRECT_ANSWER", "HYBRID_RAG", "TRANSACTION_EXEC", "REJECT_OOS")

MUTATION_VERBS = re.compile(
    r"\b(release|approve|adjust|migrate|quarantine|update|change|delete|submit|ship|set)\b", re.I
)
LOT_ID = re.compile(r"\bLOT[\s\-]?\d{1,5}\b", re.I)
SKU_ID = re.compile(r"\bSKU[\s\-]?\d{1,3}\b", re.I)
OOS = re.compile(
    r"\b(weather|forecast|stock price|share price|lottery|horoscope|poem|joke|recipe|"
    r"football|cricket score|election|celebrity)\b",
    re.I,
)
GREETING = re.compile(r"^\s*((hi|hello|hey)( there)?|thanks|thank you|good (morning|afternoon|evening))\b[\s!.?]*$", re.I)
META = re.compile(r"\b(what can you do|who are you|help me use|how do you work)\b", re.I)
DOMAIN_HINTS = [
    ("quality", re.compile(r"\b(lot|batch|qa|safety|release|excursion)\b", re.I)),
    ("catalog", re.compile(r"\b(sku|catalog|product)\b", re.I)),
    ("logistics", re.compile(r"\b(cold|storage|temperature|quarantine|refrigerat)", re.I)),
    ("commerce", re.compile(r"\b(return|refund|receipt)\b", re.I)),
    ("security", re.compile(r"\b(access|role|tenant|permission)\b", re.I)),
    ("compliance", re.compile(r"\b(pii|personal|privacy|redact)\b", re.I)),
    ("operations", re.compile(r"\b(incident|shift|handover|severity|escalat)\b", re.I)),
]


@dataclass(frozen=True)
class TriageResult:
    intent: str
    domain: str
    risk_tier: int
    complexity: str
    route: str
    confidence: float
    action: str | None = None
    source: str = "rules"


def _domain(text: str) -> str:
    for name, pat in DOMAIN_HINTS:
        if pat.search(text):
            return name
    return "general"


def classify_rules(query: str) -> TriageResult:
    text = normalize(query).strip()
    complexity = "high" if len(text.split()) > 25 or " and " in text.lower() else "low"

    if GREETING.match(text) or META.search(text):
        return TriageResult("smalltalk", "general", 1, "low", "DIRECT_ANSWER", 0.95)
    if OOS.search(text):
        return TriageResult("out_of_scope", "general", 1, complexity, "REJECT_OOS", 0.9)
    if MUTATION_VERBS.search(text) and (LOT_ID.search(text) or SKU_ID.search(text)):
        action = None
        if LOT_ID.search(text) and re.search(r"\b(release|approve|quarantine|update|change)\b", text, re.I):
            action = "lot.release"
        elif SKU_ID.search(text) and re.search(r"\bmigrate\b", text, re.I):
            action = "sku.migrate"
        else:
            action = "inventory.adjust"
        return TriageResult("mutation", _domain(text), 3, complexity, "TRANSACTION_EXEC", 0.9, action)
    if re.search(r"\?|\b(what|which|how|when|who|does|is|are|can|must|should)\b", text, re.I):
        return TriageResult("policy_question", _domain(text), 1, complexity, "HYBRID_RAG", 0.85)
    # Unclear: default to retrieval, which grounds or abstains. Low confidence.
    return TriageResult("unclear", _domain(text), 1, complexity, "HYBRID_RAG", 0.4)


def classify(query: str, llm_fallback=None) -> TriageResult:
    """Rules first. `llm_fallback(query) -> TriageResult | None` is called only on low confidence."""
    result = classify_rules(query)
    if result.confidence < 0.6 and llm_fallback is not None:
        refined = llm_fallback(query)
        if refined is not None:
            return refined
    return result


TRIAGE_SYSTEM = (
    "You route user questions for a policy assistant. Reply with JSON only, no prose: "
    '{"intent": string, "route": "DIRECT_ANSWER" | "HYBRID_RAG" | "REJECT_OOS", '
    '"domain": string, "risk_tier": 1 | 2}. '
    "DIRECT_ANSWER: greetings and questions about the assistant. "
    "HYBRID_RAG: questions about policies or procedures. "
    "REJECT_OOS: anything unrelated to operations policy. "
    "Never choose any other route."
)
# The model may never start a Tier 3 request. TRANSACTION_EXEC stays rules-only (design.md D26).
LLM_ALLOWED_ROUTES = frozenset({"DIRECT_ANSWER", "HYBRID_RAG", "REJECT_OOS"})


def parse_llm_triage(text: str, query: str) -> TriageResult | None:
    """Parse the model's JSON. Anything unexpected returns None, and the rules result is kept."""
    import json

    try:
        data = json.loads(text.strip().strip("`"))
    except (json.JSONDecodeError, AttributeError):
        return None
    route = data.get("route")
    if route not in LLM_ALLOWED_ROUTES:
        return None
    tier = data.get("risk_tier", 1)
    if tier not in (1, 2):
        tier = 1
    domain = data.get("domain") if data.get("domain") in {n for n, _ in DOMAIN_HINTS} | {"general"} else _domain(query)
    return TriageResult(
        intent=str(data.get("intent", "llm"))[:40],
        domain=domain,
        risk_tier=tier,
        complexity="low",
        route=route,
        confidence=0.7,
        source="llm",
    )
