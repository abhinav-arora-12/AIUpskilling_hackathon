"""Tool execution harness and circuit breaker (brief Gate 4, Gate 9; design.md §7).

Order for every tool call:
1. Strict Pydantic v2 validation.
2. Deterministic regex auto-repair on malformed IDs, then re-validate once (SKU99 -> SKU-099).
3. RBAC tier check (TIER_1_READ, TIER_2_COMPUTE, TIER_3_MUTATE).
4. UUIDv4 idempotency key required for TIER_3_MUTATE.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app import config as C

SKU_RE = re.compile(r"^\s*sku[\s\-_]*(\d{1,3})\s*$", re.IGNORECASE)
LOT_RE = re.compile(r"^\s*lot[\s\-_]*(\d{1,5})\s*$", re.IGNORECASE)
LOT_VALID = re.compile(r"^LOT-\d{5}$")
SKU_VALID = re.compile(r"^SKU-\d{3}$")
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class Tier(str, Enum):
    TIER_1_READ = "TIER_1_READ"
    TIER_2_COMPUTE = "TIER_2_COMPUTE"
    TIER_3_MUTATE = "TIER_3_MUTATE"


def repair_sku(value: str) -> str:
    m = SKU_RE.match(value)
    return f"SKU-{int(m.group(1)):03d}" if m else value


def repair_lot(value: str) -> str:
    m = LOT_RE.match(value)
    return f"LOT-{int(m.group(1)):05d}" if m else value


class TransactionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str = Field(pattern=r"^(lot\.release|inventory\.adjust|sku\.migrate)$")
    lot_id: str | None = Field(default=None, pattern=r"^LOT-\d{5}$")
    sku_id: str | None = Field(default=None, pattern=r"^SKU-\d{3}$")

    @model_validator(mode="after")
    def _requires_matching_id(self):
        # A Tier 3 request must name the entity it changes (no empty approvals).
        if self.action == "lot.release" and not self.lot_id:
            raise ValueError("lot.release requires lot_id")
        if self.action == "sku.migrate" and not self.sku_id:
            raise ValueError("sku.migrate requires sku_id")
        if self.action == "inventory.adjust" and not (self.lot_id or self.sku_id):
            raise ValueError("inventory.adjust requires lot_id or sku_id")
        return self


class HarnessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HarnessResult:
    args: dict
    repairs: list[str] = field(default_factory=list)


def validate_transaction(raw: dict) -> HarnessResult:
    """Validate, auto-repair once, validate again. Raises HarnessError(E_VALIDATION) on failure."""
    repairs: list[str] = []
    try:
        TransactionArgs.model_validate(raw)
        return HarnessResult(args=raw)
    except ValidationError:
        pass
    fixed = dict(raw)
    for field_name, fixer, pattern in (
        ("lot_id", repair_lot, LOT_VALID),
        ("sku_id", repair_sku, SKU_VALID),
    ):
        value = fixed.get(field_name)
        if isinstance(value, str) and not pattern.match(value):
            new = fixer(value)
            if new != value:
                repairs.append(f"{field_name}: {value!r} -> {new!r}")
                fixed[field_name] = new
    try:
        TransactionArgs.model_validate(fixed)
    except ValidationError as exc:
        raise HarnessError("E_VALIDATION", "; ".join(e["msg"] for e in exc.errors()[:3])) from exc
    return HarnessResult(args=fixed, repairs=repairs)


def check_tier(tool_tier: Tier, caller_role: str, request_only: bool = False) -> None:
    """RBAC by tier. request_only=True means the caller may only ask for a Tier 3 change, not decide it."""
    allowed = {
        "operator": {Tier.TIER_1_READ},
        "supervisor": {Tier.TIER_1_READ, Tier.TIER_2_COMPUTE},
        "reviewer": {Tier.TIER_1_READ, Tier.TIER_2_COMPUTE, Tier.TIER_3_MUTATE},
    }.get(caller_role, set())
    if request_only and tool_tier == Tier.TIER_3_MUTATE:
        allowed = allowed | {Tier.TIER_3_MUTATE} if caller_role in {"operator", "supervisor", "reviewer"} else allowed
    if tool_tier not in allowed:
        raise HarnessError("E_RBAC_DENIED", f"role '{caller_role}' may not use {tool_tier.value}")


def check_idempotency(key: str | None) -> str:
    if not key or not UUID4_RE.match(key):
        raise HarnessError("E_VALIDATION", "TIER_3 calls need a UUIDv4 idempotency key")
    return key


def canonical_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode()).hexdigest()


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


class CircuitBreaker:
    """Opens after BREAKER_MAX_IDENTICAL_FAILURES identical failing calls (design.md §7, E_BREAKER_OPEN)."""

    def __init__(self, max_failures: int = C.BREAKER_MAX_IDENTICAL_FAILURES):
        self.max_failures = max_failures
        self.failures: dict[str, int] = {}

    def is_open(self, tool: str, args: dict) -> bool:
        return self.failures.get(canonical_hash(tool, args), 0) >= self.max_failures

    def record_failure(self, tool: str, args: dict) -> None:
        h = canonical_hash(tool, args)
        self.failures[h] = self.failures.get(h, 0) + 1

    def record_success(self, tool: str, args: dict) -> None:
        self.failures.pop(canonical_hash(tool, args), None)

    def reset(self) -> None:
        self.failures.clear()
