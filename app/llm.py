"""Provider-agnostic LLM client (design.md D8, D25).

Default provider: OpenRouter (OpenAI-compatible HTTP API), model from OPENROUTER_MODEL.
Optional provider: Anthropic, used only when OPENROUTER_API_KEY is empty.
With no key, callers get LLMError and fall back to the local rules and extractive answerer.

Every call returns token usage, so KPI-29 (cost) can be measured from real counts.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app import config as C

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """Any LLM failure. Callers must fall back; the LLM is never on the critical path for safety."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    ttft_ms: float | None = None  # time to first streamed token (KPI-25); None when not streamed

    @property
    def cost_usd(self) -> float:
        if self.provider != "openrouter" or self.model not in C.OPENROUTER_PRICES:
            return 0.0  # unpriced model: reported as zero and flagged in the scorecard note
        price_in, price_out = C.OPENROUTER_PRICES[self.model]
        return self.input_tokens * price_in + self.output_tokens * price_out


def provider() -> str | None:
    if C.SETTINGS.openrouter_api_key:
        return "openrouter"
    if C.SETTINGS.anthropic_api_key:
        return "anthropic"
    return None


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 400,
    temperature: float = 0.0,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
    stream: bool = False,
) -> LLMResult:
    p = provider()
    if p is None:
        raise LLMError("no LLM key configured")
    if p == "openrouter":
        fn = _openrouter_stream if stream else _openrouter
        return fn(system, user, model or C.OPENROUTER_GENERATION_MODEL, max_tokens, temperature,
                  timeout, transport)
    return _anthropic(system, user, max_tokens, temperature, timeout)


def _openrouter_stream(system, user, model, max_tokens, temperature, timeout, transport) -> LLMResult:
    """Streamed chat completion. TTFT is measured on the first non-empty content delta."""
    import json
    import time

    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {C.SETTINGS.openrouter_api_key}", "Content-Type": "application/json",
               "X-Title": "The Unreliable Assistant"}
    parts: list[str] = []
    usage: dict = {}
    ttft = None
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            with client.stream("POST", OPENROUTER_URL, json=body, headers=headers) as r:
                if r.status_code != 200:
                    raise LLMError(f"openrouter HTTP {r.status_code}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}).get("content") if choices else None
                    if delta:
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
                        parts.append(delta)
    except httpx.HTTPError as exc:
        raise LLMError(f"openrouter transport error: {type(exc).__name__}") from exc
    return LLMResult(
        text="".join(parts).strip(),
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        model=model,
        provider="openrouter",
        ttft_ms=round(ttft, 2) if ttft is not None else None,
    )


def _openrouter(system, user, model, max_tokens, temperature, timeout, transport) -> LLMResult:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {C.SETTINGS.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "The Unreliable Assistant",
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            r = client.post(OPENROUTER_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"openrouter transport error: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise LLMError(f"openrouter HTTP {r.status_code}")  # includes 429 rate limits (FMEA)
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("openrouter response malformed") from exc
    return LLMResult(
        text=text.strip(),
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        model=model,
        provider="openrouter",
    )


def _anthropic(system, user, max_tokens, temperature, timeout) -> LLMResult:
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=C.SETTINGS.anthropic_api_key, timeout=timeout, max_retries=1)
        msg = client.messages.create(
            model=C.GENERATION_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        raise LLMError(f"anthropic error: {type(exc).__name__}") from exc
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return LLMResult(
        text=text,
        input_tokens=int(getattr(msg.usage, "input_tokens", 0)),
        output_tokens=int(getattr(msg.usage, "output_tokens", 0)),
        model=C.GENERATION_MODEL,
        provider="anthropic",
    )
