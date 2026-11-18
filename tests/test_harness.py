import uuid

import pytest

from app.agent.harness import (
    CircuitBreaker,
    HarnessError,
    Tier,
    check_idempotency,
    check_tier,
    repair_lot,
    repair_sku,
    validate_transaction,
)


@pytest.mark.parametrize("raw,fixed", [("SKU99", "SKU-099"), ("sku 7", "SKU-007"), ("SKU-099", "SKU-099")])
def test_sku_auto_repair(raw, fixed):
    assert repair_sku(raw) == fixed


@pytest.mark.parametrize("raw,fixed", [("lot 44", "LOT-00044"), ("LOT-88392", "LOT-88392"), ("lot-1", "LOT-00001")])
def test_lot_auto_repair(raw, fixed):
    assert repair_lot(raw) == fixed


def test_validation_repairs_once_and_reports_repairs():
    res = validate_transaction({"action": "lot.release", "lot_id": "lot 44"})
    assert res.args["lot_id"] == "LOT-00044"
    assert res.repairs and "lot_id" in res.repairs[0]


def test_validation_rejects_unknown_action_and_extra_fields():
    with pytest.raises(HarnessError) as exc:
        validate_transaction({"action": "db.drop", "lot_id": "LOT-00044"})
    assert exc.value.code == "E_VALIDATION"
    with pytest.raises(HarnessError):
        validate_transaction({"action": "lot.release", "lot_id": "LOT-00044", "extra": 1})


def test_rbac_operator_can_request_but_not_decide_tier3():
    check_tier(Tier.TIER_3_MUTATE, "operator", request_only=True)  # request is allowed
    with pytest.raises(HarnessError) as exc:
        check_tier(Tier.TIER_3_MUTATE, "operator")  # executing is not
    assert exc.value.code == "E_RBAC_DENIED"


def test_idempotency_requires_uuid4():
    key = str(uuid.uuid4())
    assert check_idempotency(key) == key
    with pytest.raises(HarnessError):
        check_idempotency("not-a-uuid")
    with pytest.raises(HarnessError):
        check_idempotency(None)


def test_breaker_trips_after_two_identical_failures_only():
    b = CircuitBreaker()
    args = {"query": "x", "tenant_id": "tenant_a"}
    b.record_failure("hybrid_retrieve", args)
    assert not b.is_open("hybrid_retrieve", args)
    b.record_failure("hybrid_retrieve", args)
    assert b.is_open("hybrid_retrieve", args)
    assert not b.is_open("hybrid_retrieve", {"query": "different", "tenant_id": "tenant_a"})
    b.record_success("hybrid_retrieve", args)  # success clears the count
    assert not b.is_open("hybrid_retrieve", args)


def test_tier3_request_must_name_its_entity():
    with pytest.raises(HarnessError):
        validate_transaction({"action": "lot.release"})  # no lot_id: no empty approvals
    with pytest.raises(HarnessError):
        validate_transaction({"action": "sku.migrate", "lot_id": "LOT-00044"})
    assert validate_transaction({"action": "sku.migrate", "sku_id": "SKU-007"}).args["sku_id"] == "SKU-007"
