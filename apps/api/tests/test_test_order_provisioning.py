"""Creating real Razorpay Test Mode orders for a guided experiment.

The Razorpay call itself is doubled here -- these tests are about what the
system does with the response, and about the bounds it refuses to exceed.
Whether the live Orders API works is established by actually calling it,
not by a unit test with a fake in it.

The one thing worth stating plainly: nothing in this module can pay for an
order. Creating an order and completing a payment are different
operations, and only a human in Checkout can do the second one. That is
why a cohort's outcomes are worth analysing.
"""

from __future__ import annotations

import uuid

import pytest

from provisioning.razorpay_order_client import MANUAL_CAPTURE, RazorpayOrderClient
from provisioning.test_orders import (
    MAX_ORDERS_PER_EXPERIMENT,
    CohortAlreadyInUse,
    create_test_orders,
)
from repository.payment_experiments import (
    get_experiment,
    list_experiment_orders_with_state,
)


class FakeOrderClient:
    """Returns an order object shaped like Razorpay's own response."""

    def __init__(self):
        self.calls: list[dict] = []

    def create_order(self, amount, currency="INR", receipt=None, notes=None):
        self.calls.append({"amount": amount, "currency": currency, "receipt": receipt, "notes": notes})
        order_id = f"order_FAKE{uuid.uuid4().hex[:10]}"
        return {
            "id": order_id, "entity": "order", "amount": amount, "amount_paid": 0,
            "amount_due": amount, "currency": currency, "receipt": receipt,
            "status": "created", "attempts": 0, "notes": notes or {},
        }


class ExplodingOrderClient:
    def create_order(self, *args, **kwargs):
        raise RuntimeError("Razorpay unreachable")


def test_creates_the_requested_number_of_orders_and_freezes_the_cohort(committed_merchant):
    db_conn, merchant_id = committed_merchant
    client = FakeOrderClient()

    result = create_test_orders(
        db_conn, client, merchant_id, kind="failure_pattern", count=6, amount=50000
    )

    assert len(result.orders) == 6
    assert len(client.calls) == 6
    assert [o.position for o in result.orders] == [1, 2, 3, 4, 5, 6]

    cohort = list_experiment_orders_with_state(db_conn, result.experiment_id)
    assert len(cohort) == 6, "the cohort is frozen at creation, before any outcome exists"
    assert all(row["payment_status"] is None for row in cohort), "nothing has been paid yet"


def test_the_razorpay_order_object_is_persisted_verbatim(committed_merchant):
    """What is stored is what Razorpay said, not a reshaped copy of it."""
    from repository.orders import get_order

    db_conn, merchant_id = committed_merchant
    result = create_test_orders(
        db_conn, FakeOrderClient(), merchant_id, kind="capture_decision", count=1, amount=50000
    )

    stored = get_order(db_conn, result.orders[0].order_id)
    assert stored is not None
    assert stored["raw_reference"]["entity"] == "order"
    assert stored["raw_reference"]["id"] == result.orders[0].order_id


def test_orders_are_attributed_to_the_experiment_that_created_them(committed_merchant):
    db_conn, merchant_id = committed_merchant
    result = create_test_orders(
        db_conn, FakeOrderClient(), merchant_id, kind="failure_pattern", count=2, amount=10000
    )

    experiment = get_experiment(db_conn, result.experiment_id)
    assert experiment is not None
    assert experiment["kind"] == "failure_pattern"
    assert experiment["source"] == "razorpay_test_mode", "a cohort's money provenance is recorded, not assumed"


def test_a_cohort_larger_than_the_ceiling_is_refused(committed_merchant):
    db_conn, merchant_id = committed_merchant

    with pytest.raises(ValueError, match="count must be between"):
        create_test_orders(
            db_conn, FakeOrderClient(), merchant_id, kind="failure_pattern",
            count=MAX_ORDERS_PER_EXPERIMENT + 1, amount=50000,
        )


def test_zero_orders_is_refused(committed_merchant):
    db_conn, merchant_id = committed_merchant
    with pytest.raises(ValueError):
        create_test_orders(db_conn, FakeOrderClient(), merchant_id, kind="failure_pattern", count=0, amount=50000)


def test_an_unknown_experiment_kind_is_refused(committed_merchant):
    db_conn, merchant_id = committed_merchant
    with pytest.raises(ValueError, match="kind must be"):
        create_test_orders(db_conn, FakeOrderClient(), merchant_id, kind="whatever", count=1, amount=50000)


def test_a_non_positive_amount_is_refused(committed_merchant):
    db_conn, merchant_id = committed_merchant
    with pytest.raises(ValueError, match="amount must be positive"):
        create_test_orders(db_conn, FakeOrderClient(), merchant_id, kind="capture_decision", count=1, amount=0)


def test_a_razorpay_failure_is_not_swallowed(committed_merchant):
    """No local fallback order is fabricated when Razorpay cannot be
    reached -- the caller is told."""
    db_conn, merchant_id = committed_merchant

    with pytest.raises(RuntimeError, match="Razorpay unreachable"):
        create_test_orders(db_conn, ExplodingOrderClient(), merchant_id, kind="capture_decision", count=1, amount=50000)


# ---------------------------------------------------------------------------
# Appending to a group, one real order at a time
# ---------------------------------------------------------------------------


def test_orders_can_be_appended_to_a_group_before_anything_is_paid(committed_merchant):
    """This is what lets the six-payment experiment create its orders one
    real call at a time, so a card appears only once its own order exists."""
    db_conn, merchant_id = committed_merchant
    client = FakeOrderClient()

    first = create_test_orders(
        db_conn, client, merchant_id, kind="failure_pattern", count=1, amount=50000
    )
    for _ in range(5):
        create_test_orders(
            db_conn, client, merchant_id, kind="failure_pattern", count=1, amount=50000,
            experiment_id=first.experiment_id,
        )

    cohort = list_experiment_orders_with_state(db_conn, first.experiment_id)
    assert [row["position"] for row in cohort] == [1, 2, 3, 4, 5, 6]
    assert len({row["order_id"] for row in cohort}) == 6, "six distinct real orders"


def test_appending_stops_at_the_ceiling(committed_merchant):
    db_conn, merchant_id = committed_merchant
    client = FakeOrderClient()
    first = create_test_orders(
        db_conn, client, merchant_id, kind="failure_pattern", count=6, amount=50000
    )

    with pytest.raises(ValueError, match="at most"):
        create_test_orders(
            db_conn, client, merchant_id, kind="failure_pattern", count=1, amount=50000,
            experiment_id=first.experiment_id,
        )


def test_orders_cannot_be_added_once_the_group_has_a_result(committed_merchant):
    """The denominator guard. Someone who disliked '4 of 6' must not be able
    to make it '4 of 9' by adding three fresh orders afterwards."""
    from repository.payment_attempts import insert_payment_attempt

    db_conn, merchant_id = committed_merchant
    client = FakeOrderClient()
    first = create_test_orders(
        db_conn, client, merchant_id, kind="failure_pattern", count=2, amount=50000
    )

    insert_payment_attempt(
        db_conn, payment_attempt_id=f"pay_APP_{uuid.uuid4().hex[:10]}",
        order_id=first.orders[0].order_id, status="failed", method="card", captured=False,
        error_source="bank", error_step="payment_authorization", error_reason="payment_failed",
        amount=50000, raw_reference={"test": True},
    )
    db_conn.commit()

    with pytest.raises(CohortAlreadyInUse):
        create_test_orders(
            db_conn, client, merchant_id, kind="failure_pattern", count=1, amount=50000,
            experiment_id=first.experiment_id,
        )


def test_a_group_belonging_to_another_merchant_is_refused(committed_merchant):
    db_conn, merchant_id = committed_merchant
    from repository.merchants import insert_merchant

    other_merchant = str(insert_merchant(db_conn, "Other Merchant", {}, {}))
    db_conn.commit()
    other = create_test_orders(
        db_conn, FakeOrderClient(), other_merchant, kind="failure_pattern", count=1, amount=50000
    )

    try:
        with pytest.raises(ValueError, match="different merchant"):
            create_test_orders(
                db_conn, FakeOrderClient(), merchant_id, kind="failure_pattern", count=1,
                amount=50000, experiment_id=other.experiment_id,
            )
    finally:
        with db_conn.cursor() as cur:
            cur.execute(
                "delete from payment_experiment_orders where experiment_id in "
                "(select id from payment_experiments where merchant_id = %s)", (other_merchant,))
            cur.execute("delete from payment_experiments where merchant_id = %s", (other_merchant,))
            cur.execute("delete from orders where merchant_id = %s", (other_merchant,))
            cur.execute("delete from merchants where id = %s", (other_merchant,))
        db_conn.commit()


# ---------------------------------------------------------------------------
# The order client's capability surface
# ---------------------------------------------------------------------------


def test_the_order_client_always_requests_manual_capture():
    """If Razorpay auto-captured, there would be no capture decision left
    for this system to make -- the payment would arrive already finished."""
    assert MANUAL_CAPTURE == 0


def test_the_order_client_cannot_capture_refund_or_pay_out():
    """Structural, not a promise: the methods do not exist on the class."""
    surface = {name for name in dir(RazorpayOrderClient) if not name.startswith("_")}

    assert "create_order" in surface
    for forbidden in ("capture_payment", "capture", "refund", "refund_payment", "payout", "transfer"):
        assert forbidden not in surface
