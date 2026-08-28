"""Decision orchestration DB-integration tests. Requires live Postgres.
Full chain: reconciliation -> context -> expectation -> RuleBasedEngine
-> persisted Decision -> DECISION_CREATED audit entry. No Policy/Action/
Verification.
"""

from __future__ import annotations

from typing import Any

from intelligence.calibration import upsert_calibrated_baseline
from intelligence.orchestration import NO_ERROR_REASON_LABEL, make_decision
from reconciliation.service import reconcile_order
from repository.audit import list_audit_trail_for_decision
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from repository.expectation_baselines import get_baseline


def _order_fixture(order_id: str, status: str, amount_paid: int, amount_due: int, attempts: int) -> dict[str, Any]:
    return {
        "id": order_id, "amount": 50000, "amount_paid": amount_paid, "amount_due": amount_due,
        "currency": "INR", "status": status, "attempts": attempts,
    }


def _payment_fixture(
    payment_id: str, order_id: str, status: str, captured: bool,
    error_source: str | None = None, error_step: str | None = None, error_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": payment_id, "order_id": order_id, "status": status, "amount": 50000,
        "method": "card", "captured": captured,
        "error_source": error_source, "error_step": error_step, "error_reason": error_reason,
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


def test_make_decision_for_gateway_failure_persists_retry_prompt(db_conn, demo_merchant_id):
    order_id = "order_dec_gateway"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_dec_gw", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", "pay_dec_gw")

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "RECOMMEND_RETRY_PROMPT"
    assert decision["order_id"] == order_id
    assert decision["payment_attempt_id"] == "pay_dec_gw"
    assert str(decision["event_id"]) == str(event["id"])
    assert float(decision["confidence"]) == 0.5  # zero-evidence default, no baseline set

    audit_trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    assert [a["checkpoint"] for a in audit_trail] == ["DECISION_CREATED"]


def test_make_decision_for_customer_cancelled_persists_no_action(db_conn, demo_merchant_id):
    order_id = "order_dec_cancelled"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_dec_cancel", order_id, "failed", False, "customer", "payment_authentication", "payment_cancelled")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", "pay_dec_cancel")

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "NO_ACTION"
    assert "CUSTOMER_CANCELLED" in decision["reason_codes"]


def test_make_decision_for_order_level_event_persists_no_action_and_audit(db_conn, demo_merchant_id):
    order_id = "order_dec_orderlevel"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_dec_ol", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "order.paid", order_id)

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "NO_ACTION"
    assert decision["payment_attempt_id"] is None
    assert "ORDER_LEVEL_EVENT" in decision["reason_codes"]

    audit_trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    assert len(audit_trail) == 1
    assert audit_trail[0]["checkpoint"] == "DECISION_CREATED"


def test_make_decision_uses_calibrated_expectation_when_baseline_exists(db_conn, demo_merchant_id):
    upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_failed", 0.82, 40)

    order_id = "order_dec_calibrated"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_dec_calib", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", "pay_dec_calib")

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert float(decision["confidence"]) == 0.82
    assert decision["expectation"]["source"] == "rule_v1"
    assert decision["expectation"]["sample_size"] == 40


def test_make_decision_twice_creates_two_distinct_decision_rows(db_conn, demo_merchant_id):
    order_id = "order_dec_twice"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_dec_twice", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", "pay_dec_twice")

    first_id = make_decision(db_conn, demo_merchant_id, event)
    second_id = make_decision(db_conn, demo_merchant_id, event)

    assert first_id != second_id  # no dedup at this layer, by design (Phase 2 Rev 2: a
                                   # changed mind produces a new row, never an edit)
    assert get_decision(db_conn, first_id) is not None
    assert get_decision(db_conn, second_id) is not None


def test_make_decision_for_authorized_payment_recommends_capture(db_conn, demo_merchant_id):
    # Closes the previous "hand-constructed Decision" gap: this is a
    # genuine make_decision() call on a real authorized-payment event,
    # producing a real RECOMMEND_CAPTURE Decision through RuleBasedEngine
    # -- not a test fixture.
    order_id = "order_dec_authorized_capture"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_dec_auth", order_id, "authorized", False)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.authorized", "pay_dec_auth")

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "RECOMMEND_CAPTURE"
    assert float(decision["confidence"]) == 1.0
    assert decision["reason_codes"] == ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"]
    assert decision["model_version"] == "rule_v1"
    assert decision["expected_impact"]["revenue_at_stake"] == 50000
    assert decision["payment_attempt_id"] == "pay_dec_auth"


def test_make_decision_for_captured_payment_does_not_recommend_capture(db_conn, demo_merchant_id):
    order_id = "order_dec_already_captured"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_dec_already_cap", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.captured", "pay_dec_already_cap")

    decision_id = make_decision(db_conn, demo_merchant_id, event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "NO_ACTION"
    assert "NO_RECOMMENDATION_RULE_MATCHED" in decision["reason_codes"]


def test_no_error_reason_context_does_not_create_baseline_row(db_conn, demo_merchant_id):
    order_id = "order_dec_no_baseline_pollution"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_dec_nb", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "order.paid", order_id)

    make_decision(db_conn, demo_merchant_id, event)

    assert get_baseline(db_conn, demo_merchant_id, NO_ERROR_REASON_LABEL) is None
