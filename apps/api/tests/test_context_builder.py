"""Context builder DB-integration tests. Requires live Postgres.

Uses the already-DB-proven reconciliation service to produce real
canonical_events rows (rather than hand-constructing them), so these
tests exercise the actual event shapes the reconciliation gate produces.
"""

from __future__ import annotations

from typing import Any

import pytest

from context.builder import ContextBuildError, build_context_snapshot
from domain.contracts import ProvenanceBand
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order


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
    raise AssertionError(f"no event {event_type} (entity_id={entity_id}) found among {[e['event_type'] for e in events]}")


def _field(snapshot, name: str):
    for f in snapshot.fields:
        if f.field == name:
            return f
    return None


# ---------------------------------------------------------------------------
# Payment-attempt event -> payment-scoped context
# ---------------------------------------------------------------------------

def test_build_context_snapshot_for_failed_payment_event(db_conn, demo_merchant_id):
    order_id = "order_ctx_failed"
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture("pay_ctx_1", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    events = list_events_for_order(db_conn, order_id)
    event = _find_event(events, "payment.attempt.failed", "pay_ctx_1")

    snapshot = build_context_snapshot(db_conn, event)

    assert snapshot.order_id == order_id
    assert snapshot.payment_attempt_id == "pay_ctx_1"
    amount_field = _field(snapshot, "amount")
    assert amount_field.value == 50000
    assert amount_field.band == ProvenanceBand.RAW
    assert _field(snapshot, "error_reason").value == "payment_failed"
    assert _field(snapshot, "method").value == "card"


def test_attempt_number_derived_from_payment_attempts_not_events(db_conn, demo_merchant_id):
    # A single captured attempt produces 3 canonical events for the order
    # (order.created, order.paid, payment.attempt.captured) but only 1
    # payment_attempts row. attempt_number must reflect the 1, proving it
    # is NOT sourced from the event count.
    order_id = "order_ctx_attempt_number"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_ctx_solo", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    events = list_events_for_order(db_conn, order_id)
    assert len(events) == 3  # order.created + order.paid + payment.attempt.captured

    payment_event = _find_event(events, "payment.attempt.captured", "pay_ctx_solo")
    snapshot = build_context_snapshot(db_conn, payment_event)

    assert _field(snapshot, "attempt_number").value == 1
    assert _field(snapshot, "attempt_number").band == ProvenanceBand.DERIVED


def test_attempt_number_correct_for_second_and_third_attempt(db_conn, demo_merchant_id):
    order_id = "order_ctx_multi"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 3),
        payments=[
            _payment_fixture("pay_ctx_m1", order_id, "failed", False, "customer", "payment_authentication", "payment_cancelled"),
            _payment_fixture("pay_ctx_m2", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed"),
            _payment_fixture("pay_ctx_m3", order_id, "captured", True),
        ],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    events = list_events_for_order(db_conn, order_id)

    third_event = _find_event(events, "payment.attempt.captured", "pay_ctx_m3")
    snapshot = build_context_snapshot(db_conn, third_event)
    assert _field(snapshot, "attempt_number").value == 3


# ---------------------------------------------------------------------------
# Order-level event -> order-scoped context, no payment fields forced in
# ---------------------------------------------------------------------------

def test_order_event_context_does_not_include_payment_fields(db_conn, demo_merchant_id):
    order_id = "order_ctx_orderlevel"
    client = _FakeClient(
        order=_order_fixture(order_id, "paid", 50000, 0, 1),
        payments=[_payment_fixture("pay_ctx_ol", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    events = list_events_for_order(db_conn, order_id)
    order_paid_event = _find_event(events, "order.paid", order_id)

    snapshot = build_context_snapshot(db_conn, order_paid_event)

    assert snapshot.payment_attempt_id is None
    field_names = {f.field for f in snapshot.fields}
    assert field_names == {"amount", "amount_paid", "amount_due", "status", "attempts"}
    assert "error_source" not in field_names
    assert "method" not in field_names
    assert "attempt_number" not in field_names


# ---------------------------------------------------------------------------
# Missing required fields fail loudly, never fabricated
# ---------------------------------------------------------------------------

def test_missing_amount_on_payment_event_raises(db_conn):
    fake_event = {
        "event_type": "payment.attempt.failed",
        "entity_id": "pay_fake",
        "order_id": "order_fake",
        "payload": {"method": "card"},  # amount deliberately missing
    }
    with pytest.raises(ContextBuildError, match="amount"):
        build_context_snapshot(db_conn, fake_event)


def test_missing_status_on_order_event_raises(db_conn):
    fake_event = {
        "event_type": "order.paid",
        "entity_id": "order_fake",
        "order_id": "order_fake",
        "payload": {"amount": 50000, "amount_paid": 50000, "amount_due": 0, "attempts": 1},  # status missing
    }
    with pytest.raises(ContextBuildError, match="status"):
        build_context_snapshot(db_conn, fake_event)


def test_unsupported_event_type_raises(db_conn):
    fake_event = {
        "event_type": "refund.created",  # not a real EventType member at all
        "entity_id": "x", "order_id": "x", "payload": {},
    }
    with pytest.raises(ValueError):
        # EventType(...) itself raises ValueError for an unrecognized value
        build_context_snapshot(db_conn, fake_event)
