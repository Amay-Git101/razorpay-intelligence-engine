"""Reconciliation DB-integration tests. Requires a live Postgres via
DATABASE_URL (same fixture/skip behavior as test_db_invariants.py).

Fixtures below model the ACTUAL Phase 1 VERIFIED evidence shapes:
  - the order_TUtsn4abstMa1L sequence (2 failed attempts + 1 captured,
    different failure origins: customer-side cancellation, gateway-side
    failure)
  - the manual-capture order sequence (authorized -> captured via an
    explicit capture call)

Using a FakeRazorpayClient test double that returns these shapes is
testing THIS PROJECT's reconciliation logic against known-real Razorpay
response shapes -- it is not a new claim of Razorpay capability
verification. Method is always "card" (the only VERIFIED method value);
no fixture uses the "attempted" order status (DOCUMENTED only, never
independently observed). Tests explicitly marked as an "internal
invariant" test are exercising this project's own defensive code against
an engineered/synthetic scenario, not an observed Razorpay behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.orders import get_order
from repository.payment_attempts import get_payment_attempt


class FakeRazorpayClient:
    """Test double satisfying RazorpayReadClientProtocol structurally
    (no inheritance needed). Never makes a network call."""

    def __init__(self, order: dict[str, Any], payments: list[dict[str, Any]]):
        self._order = order
        self._payments = {p["id"]: p for p in payments}

    def set_order(self, order: dict[str, Any]) -> None:
        self._order = order

    def set_payments(self, payments: list[dict[str, Any]]) -> None:
        self._payments = {p["id"]: p for p in payments}

    def get_order(self, order_id: str) -> dict[str, Any]:
        assert order_id == self._order["id"]
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        assert order_id == self._order["id"]
        return [dict(p) for p in self._payments.values()]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return dict(self._payments[payment_id])


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


# ---------------------------------------------------------------------------
# The verified order_TUtsn4abstMa1L-shaped sequence: 2 failures then 1 capture
# ---------------------------------------------------------------------------

def test_multi_attempt_failed_failed_captured_ingested(db_conn, demo_merchant_id):
    order_id = "order_recon_multi"
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="paid", amount_paid=50000, amount_due=0, attempts=3),
        payments=[
            _payment_fixture("pay_recon_1", order_id, "failed", False,
                              "customer", "payment_authentication", "payment_cancelled"),
            _payment_fixture("pay_recon_2", order_id, "failed", False,
                              "gateway", "payment_authorization", "payment_failed"),
            _payment_fixture("pay_recon_3", order_id, "captured", True),
        ],
    )

    new_events = reconcile_order(db_conn, client, demo_merchant_id, order_id)

    # order.created + order.paid + 3 payment attempt events = 5
    assert len(new_events) == 5

    for pay_id, expected_status in [("pay_recon_1", "failed"), ("pay_recon_2", "failed"), ("pay_recon_3", "captured")]:
        row = get_payment_attempt(db_conn, pay_id)
        assert row is not None
        assert row["status"] == expected_status
        assert row["order_id"] == order_id  # each attempt is a DISTINCT row under the same order

    order_row = get_order(db_conn, order_id)
    assert order_row["status"] == "paid"
    assert order_row["amount_paid"] == 50000
    assert order_row["attempts"] == 3


# ---------------------------------------------------------------------------
# Authorized -> captured via reconciliation
# ---------------------------------------------------------------------------

def test_authorized_to_captured_via_reconciliation(db_conn, demo_merchant_id):
    order_id = "order_recon_capture"
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="created", amount_paid=0, amount_due=50000, attempts=1),
        payments=[_payment_fixture("pay_recon_auth", order_id, "authorized", False)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert get_payment_attempt(db_conn, "pay_recon_auth")["status"] == "authorized"

    # Simulate the capture call having happened (Action module, later gate)
    # and the order having moved to paid.
    client.set_order(_order_fixture(order_id, status="paid", amount_paid=50000, amount_due=0, attempts=1))
    client.set_payments([_payment_fixture("pay_recon_auth", order_id, "captured", True)])

    new_events = reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert len(new_events) == 2  # order.paid + payment.attempt.captured

    row = get_payment_attempt(db_conn, "pay_recon_auth")
    assert row["status"] == "captured"
    assert row["captured"] is True


# ---------------------------------------------------------------------------
# Unchanged-status re-run is a strict no-op
# ---------------------------------------------------------------------------

def test_unchanged_status_rerun_is_noop(db_conn, demo_merchant_id):
    order_id = "order_recon_noop"
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="created", amount_paid=0, amount_due=50000, attempts=1),
        payments=[_payment_fixture("pay_recon_noop", order_id, "authorized", False)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)

    second_pass_events = reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert second_pass_events == []

    events = list_events_for_order(db_conn, order_id)
    assert len(events) == 2  # order.created + payment.attempt.authorized, not doubled


# ---------------------------------------------------------------------------
# Invalid transition -> anomaly, not a crash.
# NOTE: this is an INTERNAL IMPLEMENTATION INVARIANT test using an
# engineered/synthetic scenario (a captured payment "reverting" to
# failed) -- it is not modeling any Razorpay behavior actually observed
# in Phase 1. It proves this project's own defensive reconciliation code
# handles an impossible transition without corrupting state.
# ---------------------------------------------------------------------------

def test_invalid_transition_produces_anomaly_not_crash(db_conn, demo_merchant_id):
    order_id = "order_recon_anomaly"
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="paid", amount_paid=50000, amount_due=0, attempts=1),
        payments=[_payment_fixture("pay_recon_anomaly", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert get_payment_attempt(db_conn, "pay_recon_anomaly")["status"] == "captured"

    # Engineered impossible transition: captured -> failed on the SAME id.
    client.set_payments([_payment_fixture("pay_recon_anomaly", order_id, "failed", False)])

    new_events = reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert len(new_events) == 1  # only the anomaly event -- no order.paid re-fire, order unchanged

    # The payment_attempts row must be UNCHANGED, not overwritten.
    row = get_payment_attempt(db_conn, "pay_recon_anomaly")
    assert row["status"] == "captured"

    events = list_events_for_order(db_conn, order_id)
    anomaly_events = [e for e in events if e["event_type"] == "payment.attempt.anomaly"]
    assert len(anomaly_events) == 1
    assert anomaly_events[0]["payload"]["known_status"] == "captured"
    assert anomaly_events[0]["payload"]["fetched_status"] == "failed"


# ---------------------------------------------------------------------------
# Full reconciliation re-run idempotency across a whole multi-attempt order
# ---------------------------------------------------------------------------

def test_full_reconciliation_rerun_idempotent(db_conn, demo_merchant_id):
    order_id = "order_recon_full_idem"
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="paid", amount_paid=50000, amount_due=0, attempts=3),
        payments=[
            _payment_fixture("pay_full_1", order_id, "failed", False, "customer", "payment_authentication", "payment_cancelled"),
            _payment_fixture("pay_full_2", order_id, "failed", False, "gateway", "payment_authorization", "payment_failed"),
            _payment_fixture("pay_full_3", order_id, "captured", True),
        ],
    )

    first_pass = reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert len(first_pass) == 5

    second_pass = reconcile_order(db_conn, client, demo_merchant_id, order_id)
    assert second_pass == []

    events = list_events_for_order(db_conn, order_id)
    assert len(events) == 5  # not doubled to 10


# ---------------------------------------------------------------------------
# Order state is an aggregate reconciled independently of payment_attempts
# ---------------------------------------------------------------------------

def test_order_state_reconciled_independently_of_payment_attempts(db_conn, demo_merchant_id):
    order_id = "order_recon_aggregate"
    # amount_paid/amount_due/attempts here are deliberately NOT derivable
    # by counting the single payment attempt below -- they must come
    # straight from the fetched order representation, proving the order
    # aggregate is never inferred from payment_attempts.
    client = FakeRazorpayClient(
        order=_order_fixture(order_id, status="paid", amount_paid=50000, amount_due=0, attempts=3),
        payments=[_payment_fixture("pay_agg_1", order_id, "captured", True)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)

    order_row = get_order(db_conn, order_id)
    assert order_row["attempts"] == 3  # came from the order fetch, not from counting 1 payment row
    assert order_row["amount_paid"] == 50000
    assert order_row["amount_due"] == 0
