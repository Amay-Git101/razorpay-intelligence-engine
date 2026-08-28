"""Policy orchestration DB-integration tests. Requires live Postgres.
Real persisted Decisions -> evaluate_decision -> PolicyEvaluation +
POLICY_EVALUATED audit entry (decision_id only, action_id NULL --
Actions don't exist yet).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from intelligence.orchestration import make_decision
from policy.orchestration import NotPolicyGated, evaluate_decision
from reconciliation.service import reconcile_order
from repository.audit import list_audit_trail_for_decision
from repository.canonical_events import list_events_for_order
from repository.merchants import insert_merchant
from support import insert_capture_decision as _insert_capture_decision


def _order_fixture(order_id: str, status: str, amount_paid: int, amount_due: int, attempts: int) -> dict[str, Any]:
    return {
        "id": order_id, "amount": 50000, "amount_paid": amount_paid, "amount_due": amount_due,
        "currency": "INR", "status": status, "attempts": attempts,
    }


def _payment_fixture(payment_id: str, order_id: str, status: str, captured: bool, **error_fields: Any) -> dict[str, Any]:
    return {
        "id": payment_id, "order_id": order_id, "status": status, "amount": 50000,
        "method": "card", "captured": captured,
        "error_source": error_fields.get("error_source"),
        "error_step": error_fields.get("error_step"),
        "error_reason": error_fields.get("error_reason"),
    }


class _FakeClient:
    def __init__(self, order: dict[str, Any], payments: list[dict[str, Any]]):
        self._order = order
        self._payments = payments

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _find_event(events: list[dict[str, Any]], event_type: str, entity_id: str | None = None) -> dict[str, Any]:
    for e in events:
        if e["event_type"] == event_type and (entity_id is None or e["entity_id"] == entity_id):
            return e
    raise AssertionError(f"no event {event_type} (entity_id={entity_id}) found")


def _reconcile_captured_order(conn, merchant_id: str, order_id: str) -> None:
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture(f"pay_{order_id}", order_id, "captured", True)],
    )
    reconcile_order(conn, client, merchant_id, order_id)


# ---------------------------------------------------------------------------
# Real RECOMMEND_RETRY_PROMPT decision, produced end-to-end
# ---------------------------------------------------------------------------

def test_retry_prompt_decision_always_allowed(db_conn, demo_merchant_id):
    order_id = "order_pol_retry"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_pol_retry", order_id, "failed", False, error_source="gateway", error_step="payment_authorization", error_reason="payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", "pay_pol_retry")
    decision_id = make_decision(db_conn, demo_merchant_id, event)

    evaluation = evaluate_decision(db_conn, decision_id)

    assert evaluation.allowed is True
    assert evaluation.requires_approval is False
    assert evaluation.reason_codes == ["NO_MONEY_MOVEMENT"]

    audit_trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    checkpoints = [a["checkpoint"] for a in audit_trail]
    assert checkpoints == ["DECISION_CREATED", "POLICY_EVALUATED"]
    policy_audit = audit_trail[1]
    assert policy_audit["action_id"] is None


# ---------------------------------------------------------------------------
# NO_ACTION decisions are not policy-gated
# ---------------------------------------------------------------------------

def test_no_action_decision_raises_and_gets_no_policy_audit(db_conn, demo_merchant_id):
    order_id = "order_pol_noaction"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_pol_noaction", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "order.paid", order_id)
    decision_id = make_decision(db_conn, demo_merchant_id, event)

    with pytest.raises(NotPolicyGated):
        evaluate_decision(db_conn, decision_id)

    audit_trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    checkpoints = [a["checkpoint"] for a in audit_trail]
    assert checkpoints == ["DECISION_CREATED"]  # no POLICY_EVALUATED entry was manufactured


# ---------------------------------------------------------------------------
# Hand-constructed RECOMMEND_CAPTURE decisions exercise all three bands
# ---------------------------------------------------------------------------

def test_capture_decision_within_auto_allow_band(db_conn, demo_merchant_id):
    order_id = "order_pol_cap_auto"
    _reconcile_captured_order(db_conn, demo_merchant_id, order_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Json({"max_auto_capture_amount": 20000, "approval_band_upper": 100000}), demo_merchant_id),
        )

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, f"pay_{order_id}", amount=15000)
    evaluation = evaluate_decision(db_conn, decision_id)

    assert evaluation.allowed is True
    assert evaluation.requires_approval is False


def test_capture_decision_within_approval_band(db_conn, demo_merchant_id):
    order_id = "order_pol_cap_approval"
    _reconcile_captured_order(db_conn, demo_merchant_id, order_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Json({"max_auto_capture_amount": 20000, "approval_band_upper": 100000}), demo_merchant_id),
        )

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, f"pay_{order_id}", amount=50000)
    evaluation = evaluate_decision(db_conn, decision_id)

    assert evaluation.allowed is True
    assert evaluation.requires_approval is True


def test_capture_decision_above_hard_limit_is_blocked(db_conn, demo_merchant_id):
    order_id = "order_pol_cap_blocked"
    _reconcile_captured_order(db_conn, demo_merchant_id, order_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Json({"max_auto_capture_amount": 20000, "approval_band_upper": 100000}), demo_merchant_id),
        )

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, f"pay_{order_id}", amount=500000)
    evaluation = evaluate_decision(db_conn, decision_id)

    assert evaluation.allowed is False
    assert "AMOUNT_EXCEEDS_HARD_LIMIT" in evaluation.reason_codes

    audit_trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    assert [a["checkpoint"] for a in audit_trail] == ["POLICY_EVALUATED"]  # no DECISION_CREATED for a hand-inserted test fixture, that's expected


# ---------------------------------------------------------------------------
# Merchant policy isolation
# ---------------------------------------------------------------------------

def test_policy_config_is_isolated_per_merchant(db_conn, demo_merchant_id):
    strict_merchant_id = str(insert_merchant(
        db_conn, "Strict Merchant",
        {"max_auto_capture_amount": 1000, "approval_band_upper": 5000}, {},
    ))
    lenient_merchant_id = demo_merchant_id
    with db_conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Json({"max_auto_capture_amount": 100000, "approval_band_upper": 500000}), lenient_merchant_id),
        )

    order_a = "order_pol_iso_a"
    order_b = "order_pol_iso_b"
    _reconcile_captured_order(db_conn, strict_merchant_id, order_a)
    _reconcile_captured_order(db_conn, lenient_merchant_id, order_b)

    strict_decision = _insert_capture_decision(db_conn, strict_merchant_id, order_a, f"pay_{order_a}", amount=50000)
    lenient_decision = _insert_capture_decision(db_conn, lenient_merchant_id, order_b, f"pay_{order_b}", amount=50000)

    strict_eval = evaluate_decision(db_conn, strict_decision)
    lenient_eval = evaluate_decision(db_conn, lenient_decision)

    assert strict_eval.allowed is False  # 50000 exceeds the strict merchant's 5000 ceiling
    assert lenient_eval.allowed is True  # same amount is well within the lenient merchant's limits
    assert lenient_eval.requires_approval is False
