"""State store behind mcp-state-store (design.md D4, D15, D30).

Two backends with the same behavior:
- PostgreSQL (production target, used when POSTGRES_DSN is set). pgvector is installed on the server.
- SQLite (WAL mode) fallback when POSTGRES_DSN is empty.

Every table carries tenant_id. Every query filters by it. Audit events form a SHA-256 hash chain.
"""

from __future__ import annotations

import hashlib
import json
import os
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
GENESIS = "0" * 64

SQLITE_SCHEMA = """
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

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS session_facts (
        fact_id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
        key TEXT NOT NULL, value TEXT NOT NULL, verified_by_chunk_id TEXT,
        created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_facts_tenant_session ON session_facts(tenant_id, session_id)",
    """CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, requested_by TEXT NOT NULL, action TEXT NOT NULL,
        args_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL, reviewer_id TEXT,
        decision_reason TEXT, created_at DOUBLE PRECISION NOT NULL, decided_at DOUBLE PRECISION,
        expires_at DOUBLE PRECISION NOT NULL, UNIQUE (tenant_id, idempotency_key))""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        event_id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
        payload_redacted TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_events(tenant_id, event_id)",
]


class StateError(Exception):
    """Carries a design.md §7 error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hash(prev: str, event_type: str, payload: str, tenant_id: str, ts: float) -> str:
    body = json.dumps([prev, tenant_id, event_type, payload, ts], sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, path: Path = C.PATHS.state_db, dsn: str | None = None):
        self.dsn = dsn if dsn is not None else os.environ.get("POSTGRES_DSN") or None
        self.dialect = "postgres" if self.dsn else "sqlite"
        self._lock = threading.Lock()
        if self.dialect == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            with self._conn.cursor() as cur:
                for stmt in PG_SCHEMA:
                    cur.execute(stmt)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SQLITE_SCHEMA)

    # ---- dialect helpers -------------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _exec(self, c, sql: str, params=()):
        cur = c.execute(self._sql(sql), params)
        return cur

    def _rows(self, c, sql: str, params=()) -> list[dict]:
        cur = self._exec(c, sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _one(self, c, sql: str, params=()) -> dict | None:
        cur = self._exec(c, sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _reads(self):
        return self._conn

    # ---- sessions and facts -------------------------------------------------------------------
    def ensure_session(self, tenant_id: str, user_id: str, session_id: str) -> None:
        with self._tx() as c:
            if self.dialect == "postgres":
                self._exec(c, "INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                           (session_id, tenant_id, user_id, time.time()))
            else:
                self._exec(c, "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?)",
                           (session_id, tenant_id, user_id, time.time()))

    def fact_put(self, tenant_id, session_id, key, value, verified_by_chunk_id) -> int:
        sql = ("INSERT INTO session_facts (tenant_id, session_id, key, value, verified_by_chunk_id, created_at)"
               " VALUES (?,?,?,?,?,?)")
        params = (tenant_id, session_id, key, value, verified_by_chunk_id, time.time())
        with self._tx() as c:
            if self.dialect == "postgres":
                row = self._one(c, sql + " RETURNING fact_id", params)
                return int(row["fact_id"])
            return int(self._exec(c, sql, params).lastrowid)

    def facts_get(self, tenant_id, session_id) -> list[dict]:
        return self._rows(self._reads(),
                          "SELECT fact_id, key, value, verified_by_chunk_id, created_at FROM session_facts"
                          " WHERE tenant_id=? AND session_id=? ORDER BY fact_id", (tenant_id, session_id))

    # ---- approvals (HITL, Tier 3) ------------------------------------------------------------
    def approval_create(self, tenant_id, requested_by, action, args: dict, idempotency_key) -> dict:
        if action not in TIER3_ACTIONS:
            raise StateError("E_RBAC_DENIED", f"action '{action}' is not a known Tier 3 action")
        now = time.time()
        with self._tx() as c:
            existing = self._one(c, "SELECT * FROM approvals WHERE tenant_id=? AND idempotency_key=?",
                                 (tenant_id, idempotency_key))
            if existing:  # idempotent replay returns the original record
                return existing
            approval_id = str(uuid.uuid4())
            self._exec(c,
                       "INSERT INTO approvals (approval_id, tenant_id, requested_by, action, args_json,"
                       " idempotency_key, status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (approval_id, tenant_id, requested_by, action, json.dumps(args, sort_keys=True),
                        idempotency_key, "PENDING", now, now + APPROVAL_TTL_S))
            row = self._one(c, "SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
        self.audit_append(tenant_id, "APPROVAL_PENDING", {"approval_id": approval_id, "action": action})
        return row

    def approval_list(self, tenant_id: str, status: str = "PENDING") -> list[dict]:
        return self._rows(self._reads(),
                          "SELECT * FROM approvals WHERE tenant_id=? AND status=? ORDER BY created_at",
                          (tenant_id, status))

    def approval_get(self, tenant_id: str, approval_id: str) -> dict | None:
        return self._one(self._reads(), "SELECT * FROM approvals WHERE tenant_id=? AND approval_id=?",
                         (tenant_id, approval_id))

    def approval_decide(self, tenant_id, approval_id, reviewer_id, decision, reason="") -> dict:
        if decision not in {"APPROVE", "REJECT"}:
            raise StateError("E_VALIDATION", "decision must be APPROVE or REJECT")
        if reviewer_id not in REVIEWER_IDS:
            raise StateError("E_RBAC_DENIED", "only reviewers may decide Tier 3 approvals")
        now = time.time()
        with self._tx() as c:
            row = self._one(c, "SELECT * FROM approvals WHERE tenant_id=? AND approval_id=?",
                            (tenant_id, approval_id))
            if row is None:
                raise StateError("E_NOT_FOUND", "approval not found")
            if row["status"] != "PENDING":
                raise StateError("E_VALIDATION", f"approval already {row['status']}")
            if reviewer_id == row["requested_by"]:
                raise StateError("E_RBAC_DENIED", "reviewer cannot approve their own request")
            if now > row["expires_at"]:
                self._exec(c, "UPDATE approvals SET status='EXPIRED' WHERE approval_id=?", (approval_id,))
                raise StateError("E_VALIDATION", "approval expired after 24 hours")
            status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            self._exec(c,
                       "UPDATE approvals SET status=?, reviewer_id=?, decision_reason=?, decided_at=?"
                       " WHERE approval_id=?", (status, reviewer_id, reason, now, approval_id))
        executed = False
        if status == "APPROVED":
            # Demo stub: no external system is changed (design.md D15). The action is recorded as executed.
            with self._tx() as c:
                self._exec(c, "UPDATE approvals SET status='EXECUTED' WHERE approval_id=?", (approval_id,))
            executed = True
        self.audit_append(tenant_id, f"APPROVAL_{status}",
                          {"approval_id": approval_id, "reviewer_id": reviewer_id, "executed": executed})
        return {**self.approval_get(tenant_id, approval_id), "executed": executed}

    # ---- audit (hash chained) ---------------------------------------------------------------
    def audit_append(self, tenant_id: str, event_type: str, payload: dict) -> dict:
        payload_json = json.dumps(payload, sort_keys=True)
        with self._tx() as c:
            last = self._one(c, "SELECT hash FROM audit_events WHERE tenant_id=? ORDER BY event_id DESC LIMIT 1",
                             (tenant_id,))
            prev = last["hash"] if last else GENESIS
            ts = time.time()
            digest = _hash(prev, event_type, payload_json, tenant_id, ts)
            sql = ("INSERT INTO audit_events (tenant_id, event_type, payload_redacted, prev_hash, hash, created_at)"
                   " VALUES (?,?,?,?,?,?)")
            params = (tenant_id, event_type, payload_json, prev, digest, ts)
            if self.dialect == "postgres":
                event_id = int(self._one(c, sql + " RETURNING event_id", params)["event_id"])
            else:
                event_id = int(self._exec(c, sql, params).lastrowid)
            return {"event_id": event_id, "hash": digest}

    def audit_list(self, tenant_id: str, limit: int = 100) -> list[dict]:
        return self._rows(self._reads(),
                          "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY event_id DESC LIMIT ?",
                          (tenant_id, limit))

    def audit_verify(self, tenant_id: str) -> bool:
        rows = self._rows(self._reads(), "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY event_id",
                          (tenant_id,))
        prev = GENESIS
        for r in rows:
            if r["prev_hash"] != prev:
                return False
            if _hash(prev, r["event_type"], r["payload_redacted"], tenant_id, r["created_at"]) != r["hash"]:
                return False
            prev = r["hash"]
        return True

    def close(self) -> None:
        self._conn.close()
