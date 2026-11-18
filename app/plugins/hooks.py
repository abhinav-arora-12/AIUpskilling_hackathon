"""Lifecycle hooks and the guards they run (design.md D14, brief Gate 0 plugin table).

A hook is an ordered entry in REGISTRY with a stage, priority, and fail_closed flag.
- PII redactor (PRE_EXECUTION, priority 10, fail closed)
- Injection shield (PRE_EXECUTION, priority 20, fail closed)
- Chunk injection scan (used on retrieved chunks before they reach the prompt, R10)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

# --- normalization --------------------------------------------------------------------------
_SEP_AT = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\}| at )\s*", re.IGNORECASE)
_SEP_DOT = re.compile(r"\s*(?:\[dot\]|\(dot\)| dot )\s*", re.IGNORECASE)


def normalize(text: str) -> str:
    """NFKC folds homoglyphs (full-width and look-alike forms) before any pattern matching."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("​", "").replace("‌", "").replace("‍", "").replace("﻿", "")
    return text


# --- PII ------------------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Spaced or dashed digit runs: 7+ digits with optional separators, e.g. "555 123 4567".
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s\-\.]?){7,}\d(?!\w)")
# Digits written as "5 5 5 1 2 3" are caught by PHONE_RE only if long enough; the spaced run below
# covers the one-digit-per-token case.
SPACED_DIGITS_RE = re.compile(r"(?<!\d)(?:\d\s){6,}\d(?!\d)")


def redact_pii(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []
    text = normalize(text)
    # Deobfuscate the email separators before matching so name[at]domain is caught.
    deob = _SEP_DOT.sub(".", _SEP_AT.sub("@", text))
    if EMAIL_RE.search(deob):
        findings.append("email")
        deob = EMAIL_RE.sub("[REDACTED_EMAIL]", deob)
    if SSN_RE.search(deob):
        findings.append("ssn")
        deob = SSN_RE.sub("[REDACTED_SSN]", deob)
    if SPACED_DIGITS_RE.search(deob):
        findings.append("phone")
        deob = SPACED_DIGITS_RE.sub("[REDACTED_PHONE]", deob)
    if PHONE_RE.search(deob):
        findings.append("phone")
        deob = PHONE_RE.sub("[REDACTED_PHONE]", deob)
    return deob, findings


# --- injection ------------------------------------------------------------------------------
INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|prompt|policy|policies)\b", re.I)),
    ("system_prompt_extraction", re.compile(r"\b(reveal|print|show|repeat|output|display|leak)\b.{0,40}\b(system prompt|hidden prompt|your instructions|developer message)\b", re.I)),
    ("role_hijack", re.compile(r"\b(you are now|act as (an? )?(unrestricted|admin|root|developer|system|jailbroken|dan)\b|pretend (to be|you are)|developer mode|jailbreak|\bDAN\b)", re.I)),
    ("tool_abuse", re.compile(r"\b(call|invoke|run)\b.{0,30}\b(tool|function)\b.{0,40}\b(without|bypass|skip)\b.{0,30}\b(approval|review|hitl)\b", re.I)),
    ("sqli", re.compile(r"(\bunion\b\s+\bselect\b|\bdrop\s+table\b|'\s*or\s+1\s*=\s*1|;\s*--|\bdelete\s+from\b)", re.I)),
]


def scan_injection(text: str) -> list[str]:
    norm = normalize(text)
    return [name for name, pat in INJECTION_PATTERNS if pat.search(norm)]


# --- registry -------------------------------------------------------------------------------
@dataclass
class HookResult:
    blocked: bool = False
    reason: str | None = None
    text: str | None = None
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hook:
    name: str
    stage: str
    priority: int
    fail_closed: bool
    fn: Callable[[str], HookResult]


def _pii_hook(text: str) -> HookResult:
    redacted, findings = redact_pii(text)
    return HookResult(text=redacted, findings=[f"pii:{f}" for f in findings])


def _injection_hook(text: str) -> HookResult:
    hits = scan_injection(text)
    if hits:
        return HookResult(blocked=True, reason="injection:" + ",".join(hits), findings=hits)
    return HookResult(text=text)


REGISTRY: list[Hook] = sorted(
    [
        Hook("pii_redactor", "PRE_EXECUTION", 10, True, _pii_hook),
        Hook("injection_shield", "PRE_EXECUTION", 20, True, _injection_hook),
    ],
    key=lambda h: h.priority,
)


def run_pre_hooks(text: str) -> tuple[HookResult, list[dict]]:
    """Run PRE_EXECUTION hooks in priority order. Exceptions in fail_closed hooks block the request."""
    trace: list[dict] = []
    current = text
    for hook in REGISTRY:
        if hook.stage != "PRE_EXECUTION":
            continue
        try:
            result = hook.fn(current)
        except Exception as exc:  # fail closed: any error blocks
            if hook.fail_closed:
                return HookResult(blocked=True, reason=f"hook_error:{hook.name}:{type(exc).__name__}"), trace
            continue
        trace.append({"hook": hook.name, "blocked": result.blocked, "findings": result.findings})
        if result.blocked:
            return result, trace
        if result.text is not None:
            current = result.text
    return HookResult(text=current), trace


def scan_chunks(chunks: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop retrieved chunks that contain instructions (R10: indirect injection)."""
    kept, dropped = [], []
    for c in chunks:
        if scan_injection(c["text"]):
            dropped.append(c["chunk_id"])
        else:
            kept.append(c)
    return kept, dropped
