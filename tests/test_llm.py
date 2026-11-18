import json

import httpx
import pytest

from app import config as C
from app import llm
from app.agent import generate as G
from app.agent.triage import TRIAGE_SYSTEM, parse_llm_triage


def openrouter_transport(content: str, prompt=120, completion=30, status=200):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        if status != 200:
            return httpx.Response(status, json={"error": "rate limited"})
        if seen["body"].get("stream"):
            # Server-sent events: content split into two deltas, then usage, then [DONE].
            half = len(content) // 2
            events = [
                {"choices": [{"delta": {"content": content[:half]}}]},
                {"choices": [{"delta": {"content": content[half:]}}]},
                {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
            ]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        })

    return httpx.MockTransport(handler), seen


@pytest.fixture()
def openrouter_key(monkeypatch):
    monkeypatch.setattr(C.SETTINGS, "openrouter_api_key", "test-key")
    monkeypatch.setattr(C.SETTINGS, "anthropic_api_key", None)


def test_no_key_means_no_provider(monkeypatch):
    monkeypatch.setattr(C.SETTINGS, "openrouter_api_key", None)
    monkeypatch.setattr(C.SETTINGS, "anthropic_api_key", None)
    assert llm.provider() is None
    with pytest.raises(llm.LLMError):
        llm.chat("s", "u")


def test_openrouter_is_default_when_key_set(openrouter_key):
    assert llm.provider() == "openrouter"


def test_openrouter_request_shape_and_usage(openrouter_key):
    transport, seen = openrouter_transport("hello", prompt=100, completion=20)
    res = llm.chat("sys", "usr", transport=transport)
    assert res.text == "hello" and res.input_tokens == 100 and res.output_tokens == 20
    assert seen["body"]["model"] == C.OPENROUTER_GENERATION_MODEL == "anthropic/claude-sonnet-5.5"
    assert seen["body"]["temperature"] == 0.0
    assert seen["auth"] == "Bearer test-key"


def test_cost_uses_configured_prices(openrouter_key):
    transport, _ = openrouter_transport("x", prompt=1000, completion=100)
    res = llm.chat("s", "u", transport=transport)
    pin, pout = C.OPENROUTER_PRICES[C.OPENROUTER_GENERATION_MODEL]
    expected = 1000 * pin + 100 * pout
    assert res.cost_usd == pytest.approx(expected)


def test_http_429_raises_llm_error_for_fallback(openrouter_key):
    transport, _ = openrouter_transport("", status=429)
    with pytest.raises(llm.LLMError):
        llm.chat("s", "u", transport=transport)


def test_llm_draft_is_grounded_and_cited(monkeypatch, openrouter_key):
    text = "QA sign-off is required within 48 hours of test completion. [POL-LOT-001#c0]"
    transport, _ = openrouter_transport(text)
    real = llm.chat
    monkeypatch.setattr(llm, "chat", lambda *a, **k: real(*a, transport=transport, **k))
    chunks = [{"chunk_id": "POL-LOT-001#c0", "text": "QA sign-off is required within 48 hours of test completion."}]
    draft, res = G.llm_draft("What is the QA sign-off window?", chunks)
    assert draft.generator == "llm" and draft.citations == ["POL-LOT-001#c0"]
    passed, _ = G.verify(draft, chunks)
    assert passed


def test_llm_not_in_policy_becomes_empty_draft(monkeypatch, openrouter_key):
    transport, _ = openrouter_transport("NOT_IN_POLICY")
    real = llm.chat
    monkeypatch.setattr(llm, "chat", lambda *a, **k: real(*a, transport=transport, **k))
    draft, _ = G.llm_draft("q", [{"chunk_id": "POL-LOT-001#c0", "text": "t"}])
    assert draft.answer == ""


def test_llm_cannot_start_tier3_request():
    assert parse_llm_triage('{"route": "TRANSACTION_EXEC", "risk_tier": 3}', "release lot 44") is None


def test_llm_triage_parses_allowed_route():
    t = parse_llm_triage('{"intent": "greeting", "route": "DIRECT_ANSWER", "domain": "general", "risk_tier": 1}',
                         "hi there")
    assert t.route == "DIRECT_ANSWER" and t.source == "llm"


def test_llm_triage_garbage_is_ignored():
    assert parse_llm_triage("not json at all", "q") is None
    assert parse_llm_triage('{"route": "NUKE"}', "q") is None


def test_triage_prompt_forbids_tier3():
    assert "Never choose any other route" in TRIAGE_SYSTEM


def test_verifier_vetoes_claim_not_in_cited_chunk(monkeypatch, openrouter_key):
    text = "Lots need a second reviewer after 30 days. [POL-LOT-001#c0]"
    transport, _ = openrouter_transport(text)
    real = llm.chat
    monkeypatch.setattr(llm, "chat", lambda *a, **k: real(*a, transport=transport, **k))
    chunks = [{"chunk_id": "POL-LOT-001#c0", "text": "QA sign-off is required within 48 hours of test completion."}]
    draft, _ = G.llm_draft("q", chunks)
    passed, claims = G.verify(draft, chunks)
    assert passed is False  # unsupported claim is vetoed, so the gateway falls back


def test_streamed_call_records_time_to_first_token(openrouter_key):
    transport, _ = openrouter_transport("QA sign-off is 48 hours.", prompt=50, completion=8)
    res = llm.chat("s", "u", transport=transport, stream=True)
    assert res.text == "QA sign-off is 48 hours."
    assert res.ttft_ms is not None and res.ttft_ms >= 0
    assert res.input_tokens == 50 and res.output_tokens == 8


def test_goal_anchor_is_first_in_generation_prompt(monkeypatch, openrouter_key):
    from app.agent.gateway import Gateway
    gw = Gateway.__new__(Gateway)
    gw.turns = {"s1": ["first question", "second question", "third question"]}
    anchor = gw._goal_anchor("current question", "tenant_a", "s1")
    assert anchor.startswith("GoalAnchorBlock (immutable):")
    assert "original_intent: current question" in anchor
    assert "second question" in anchor and "third question" in anchor
    assert "first question" not in anchor  # sliding window: last 2 prior turns only
