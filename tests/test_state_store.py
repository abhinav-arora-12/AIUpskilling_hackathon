import time
import uuid

import pytest

import app.state_store as ss
from app.state_store import StateError, StateStore


@pytest.fixture()
def store(tmp_path):
    return StateStore(tmp_path / "t.sqlite3", dsn="")  # explicit SQLite so .env POSTGRES_DSN is not used


def new_req(store, tenant="tenant_a", by="operator-7", action="lot.release", args=None):
    return store.approval_create(tenant, by, action, args or {"lot_id": "LOT-00044"}, str(uuid.uuid4()))


def test_request_is_pending_and_not_executed(store):
    row = new_req(store)
    assert row["status"] == "PENDING"
    assert store.approval_list("tenant_a")[0]["approval_id"] == row["approval_id"]


def test_idempotent_replay_returns_same_record(store):
    key = str(uuid.uuid4())
    a = store.approval_create("tenant_a", "op", "lot.release", {"lot_id": "LOT-00044"}, key)
    b = store.approval_create("tenant_a", "op", "lot.release", {"lot_id": "LOT-00044"}, key)
    assert a["approval_id"] == b["approval_id"]
    assert len(store.approval_list("tenant_a")) == 1


def test_approve_executes_and_audits(store):
    row = new_req(store)
    res = store.approval_decide("tenant_a", row["approval_id"], "reviewer-1", "APPROVE", "ok")
    assert res["status"] == "EXECUTED" and res["executed"] is True
    events = [e["event_type"] for e in store.audit_list("tenant_a")]
    assert "APPROVAL_APPROVED" in events and "APPROVAL_PENDING" in events


def test_reviewer_cannot_approve_own_request(store):
    row = new_req(store, by="reviewer-1")
    with pytest.raises(StateError) as exc:
        store.approval_decide("tenant_a", row["approval_id"], "reviewer-1", "APPROVE")
    assert exc.value.code == "E_RBAC_DENIED"


def test_non_reviewer_cannot_decide(store):
    row = new_req(store)
    with pytest.raises(StateError) as exc:
        store.approval_decide("tenant_a", row["approval_id"], "operator-7", "APPROVE")
    assert exc.value.code == "E_RBAC_DENIED"


def test_cross_tenant_decision_is_not_found(store):
    row = new_req(store, tenant="tenant_a")
    with pytest.raises(StateError) as exc:
        store.approval_decide("tenant_b", row["approval_id"], "reviewer-1", "APPROVE")
    assert exc.value.code == "E_NOT_FOUND"
    assert store.approval_list("tenant_b") == []


def test_expired_approval_cannot_be_approved(store, monkeypatch):
    row = new_req(store)
    future = time.time() + ss.APPROVAL_TTL_S + 5
    monkeypatch.setattr(ss.time, "time", lambda: future)
    with pytest.raises(StateError) as exc:
        store.approval_decide("tenant_a", row["approval_id"], "reviewer-1", "APPROVE")
    assert "expired" in str(exc.value)


def test_unknown_tier3_action_rejected(store):
    with pytest.raises(StateError) as exc:
        store.approval_create("tenant_a", "op", "db.drop", {}, str(uuid.uuid4()))
    assert exc.value.code == "E_RBAC_DENIED"


def test_audit_chain_verifies_and_detects_tampering(store):
    store.audit_append("tenant_a", "EVENT_ONE", {"x": 1})
    store.audit_append("tenant_a", "EVENT_TWO", {"x": 2})
    assert store.audit_verify("tenant_a")
    store._conn.execute(
        "UPDATE audit_events SET payload_redacted='{\"x\": 999}' WHERE event_type='EVENT_ONE' AND tenant_id='tenant_a'"
    )
    assert not store.audit_verify("tenant_a")


def test_facts_are_tenant_scoped(store):
    store.fact_put("tenant_a", "s1", "k", "v", "POL-LOT-001#c0")
    assert store.facts_get("tenant_a", "s1")[0]["value"] == "v"
    assert store.facts_get("tenant_b", "s1") == []
