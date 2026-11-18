import pytest

from app.agent.triage import classify, classify_rules


@pytest.mark.parametrize("query,route", [
    ("hello", "DIRECT_ANSWER"),
    ("What can you do?", "DIRECT_ANSWER"),
    ("What is the weather in Paris tomorrow?", "REJECT_OOS"),
    ("Please release lot 44 now", "TRANSACTION_EXEC"),
    ("Migrate SKU99 to the new format", "TRANSACTION_EXEC"),
    ("What is the QA sign-off window for releasing a lot?", "HYBRID_RAG"),
    ("How many SKUs are in the catalog?", "HYBRID_RAG"),
])
def test_rules_route(query, route):
    assert classify_rules(query).route == route


def test_mutation_is_tier3_with_action():
    t = classify_rules("Please release LOT-00044")
    assert t.risk_tier == 3 and t.action == "lot.release"


def test_low_confidence_uses_llm_only_when_provided():
    calls = []

    def llm(q):
        calls.append(q)
        return None  # refused / unavailable: keep rules result

    t = classify("Verify batch LOT-88392 safety protocol and return triage status", llm_fallback=llm)
    assert calls and t.route == "HYBRID_RAG"  # low-confidence default stays safe
    classify("hello", llm_fallback=llm)
    assert len(calls) == 1  # high-confidence rule did not call the LLM
