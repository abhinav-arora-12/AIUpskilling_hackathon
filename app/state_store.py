"""SQLite state store behind mcp-state-store (design.md D4, D15).

Every table carries tenant_id. Every query filters by it. Audit events form a SHA-256 hash chain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import config as C

APPROVAL_TTL_S = 24 * 3600
REVIEWER_IDS = frozenset({"reviewer-1", "reviewer-2"})  # demo roster (POL-ACC-001)
TIER3_ACTIONS = frozenset({"lot.release", "inventory.adjust", "sku.migrate"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
    key TEXT NOT NULL, value TEXT NOT NULL, verified_by_chunk_id TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_facts_tenant_session ON session_facts(tenant_id, session_id);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, requested_by TEXT NOT NULL, action TEXT NOT NULL,
    args_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL, reviewer_id TEXT,
    decision_reason TEXT, created_at REAL NOT NULL, decided_at REAL, expires_at REAL NOT NULL,
    UNIQUE (tenant_id, idempotency_key));
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
    payload_redacted TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_events(tenant_id, event_id);
"""

GENESIS = "0" * 64


class StateError(Exception):
    """Carries a design.md §7 error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hash(prev: str, event_type: str, payload: str, tenant_id: str, ts: float) -> str:
    body = json.dumps([prev, tenant_id, event_type, payload, ts], sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, path: Path = C.PATHS.state_db):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---- sessions and facts -------------------------------------------------
    def ensure_session(self, tenant_id: str, user_id: str, session_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?)",
                (session_id, tenant_id, user_id, time.time()),
            )

    def fact_put(self, tenant_id, session_id, key, value, verified_by_chunk_id) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO session_facts (tenant_id, session_id, key, value, verified_by_chunk_id, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, session_id, key, value, verified_by_chunk_id, time.time()),
            )
            return int(cur.lastrowid)

    def facts_get(self, tenant_id, session_id) -> list[dict]:
        rows = self._conn.execute(
            "SELECT fact_id, key, value, verified_by_chunk_id, created_at FROM session_facts"
            " WHERE tenant_id=? AND session_id=? ORDER BY fact_id",
            (tenant_id, session_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- approvals (HITL, Tier 3) ------------------------------------------
    def approval_create(self, tenant_id, requested_by, action, args: dict, idempotency_key) -> dict:
        if action not in TIER3_ACTIONS:
            raise StateError("E_RBAC_DENIED", f"action '{action}' is not a known Tier 3 action")
        now = time.time()
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing:  # idempotent replay returns the original record
                return dict(existing)
            approval_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO approvals (approval_id, tenant_id, requested_by, action, args_json,"
                " idempotency_key, status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    approval_id, tenant_id, requested_by, action, json.dumps(args, sort_keys=True),
                    idempotency_key, "PENDING", now, now + APPROVAL_TTL_S,
                ),
            )
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        self.audit_append(tenant_id, "APPROVAL_PENDING", {"approval_id": approval_id, "action": action})
        return dict(row)

    def approval_list(self, tenant_id: str, status: str = "PENDING") -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE tenant_id=? AND status=? ORDER BY created_at",
            (tenant_id, status),
        ).fetchall()
        return [dict(r) for r in rows]

    def approval_get(self, tenant_id: str, approval_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE tenant_id=? AND approval_id=?", (tenant_id, approval_id)
        ).fetchone()
        return dict(row) if row else None

    def approval_decide(self, tenant_id, approval_id, reviewer_id, decision, reason="") -> dict:
        if decision not in {"APPROVE", "REJECT"}:
            raise StateError("E_VALIDATION", "decision must be APPROVE or REJECT")
        if reviewer_id not in REVIEWER_IDS:
            raise StateError("E_RBAC_DENIED", "only reviewers may decide Tier 3 approvals")
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND approval_id=?", (tenant_id, approval_id)
            ).fetchone()
            if row is None:
                raise StateError("E_NOT_FOUND", "approval not found")
            if row["status"] != "PENDING":
                raise StateError("E_VALIDATION", f"approval already {row['status']}")
            if reviewer_id == row["requested_by"]:
                raise StateError("E_RBAC_DENIED", "reviewer cannot approve their own request")
            if now > row["expires_at"]:
                c.execute("UPDATE approvals SET status='EXPIRED' WHERE approval_id=?", (approval_id,))
                raise StateError("E_VALIDATION", "approval expired after 24 hours")
            status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            c.execute(
                "UPDATE approvals SET status=?, reviewer_id=?, decision_reason=?, decided_at=?"
                " WHERE approval_id=?",
                (status, reviewer_id, reason, now, approval_id),
            )
        executed = False
        if status == "APPROVED":
            # Demo stub: no external system is changed (design.md D15). The action is recorded as executed.
            with self._tx() as c:
                c.execute("UPDATE approvals SET status='EXECUTED' WHERE approval_id=?", (approval_id,))
            executed = True
        self.audit_append(
            tenant_id,
            f"APPROVAL_{status}",
            {"approval_id": approval_id, "reviewer_id": reviewer_id, "executed": executed},
        )
        final = self.approval_get(tenant_id, approval_id)
        return {**final, "executed": executed}

    # ---- audit (hash chained) ----------------------------------------------
    def audit_append(self, tenant_id: str, event_type: str, payload: dict) -> dict:
        payload_json = json.dumps(payload, sort_keys=True)
        with self._tx() as c:
            last = c.execute(
                "SELECT hash FROM audit_events WHERE tenant_id=? ORDER BY event_id DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            prev = last["hash"] if last else GENESIS
            ts = time.time()
            digest = _hash(prev, event_type, payload_json, tenant_id, ts)
            cur = c.execute(
                "INSERT INTO audit_events (tenant_id, event_type, payload_redacted, prev_hash, hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, event_type, payload_json, prev, digest, ts),
            )
            return {"event_id": int(cur.lastrowid), "hash": digest}

    def audit_list(self, tenant_id: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY event_id DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def audit_verify(self, tenant_id: str) -> bool:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY event_id", (tenant_id,)
        ).fetchall()
        prev = GENESIS
        for r in rows:
            if r["prev_hash"] != prev:
                return False
            if _hash(prev, r["event_type"], r["payload_redacted"], tenant_id, r["created_at"]) != r["hash"]:
                return False
            prev = r["hash"]
        return True
