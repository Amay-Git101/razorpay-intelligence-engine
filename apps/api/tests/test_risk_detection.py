"""Revenue-at-risk detection, against a real database.

The tests here are almost entirely about the DENOMINATOR. `revenue_at_risk` is
what every recovery percentage this system reports is divided by, so an error
that inflates it makes the whole product look better than it is. Each test
below corresponds to a specific way that could happen.
"""

from __future__ import annotations

import uuid

import pytest

from repository.merchants import insert_merchant
from repository.orders import upsert_order
from repository.payment_attempts import insert_payment_attempt
from risk.detection import SOURCE_SYNTHETIC, detect_revenue_at_risk


def _merchant(db_conn) -> str:
    return str(insert_merchant(db_conn, "Detection Test Merchant", {}, {}))


def _order(db_conn, merchant_id: str, amount: int, status: str = "attempted", amount_paid: int = 0) -> str:
    order_id = f"order_DET_{uuid.uuid4().hex[:10]}"
    upsert_order(
        db_conn, order_id=order_id, merchant_id=merchant_id, amount=amount,
        amount_paid=amount_paid, amount_due=amount - amount_paid, status=status,
        attempts=1, currency="INR", raw_reference={"test": True},
    )
    return order_id


def _attempt(db_conn, order_id: str, amount: int, status: str = "failed", captured: bool = False) -> str:
    payment_id = f"pay_DET_{uuid.uuid4().hex[:10]}"
    insert_payment_attempt(
        db_conn, payment_attempt_id=payment_id, order_id=order_id, status=status,
        method="card", captured=captured,
        error_source="bank" if status == "failed" else None,
        error_step="payment_authorization" if status == "failed" else None,
        error_reason="payment_failed" if status == "failed" else None,
        amount=amount, raw_reference={"test": True},
    )
    return payment_id


def test_an_order_with_several_failed_attempts_is_counted_once(db_conn):
    """THE anti-double-count test. Three failed attempts on one Rs 1,000 order
    is Rs 1,000 at risk, not Rs 3,000. Getting this wrong would inflate the
    denominator by roughly the average retry rate and quietly flatter every
    recovery figure the system publishes."""
    merchant_id = _merchant(db_conn)
    order_id = _order(db_conn, merchant_id, 100_000)
    for _ in range(3):
        _attempt(db_conn, order_id, 100_000)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)

    assert result.detected_count == 1
    assert result.revenue_at_risk == 100_000
    assert result.items[0].prior_attempt_count == 2, "the other two attempts are the retry-budget input"


def test_the_latest_failed_attempt_is_the_one_kept(db_conn):
    """The kept attempt carries the evidence the diagnosis is based on, so it
    must be the most recent failure, not an arbitrary one."""
    merchant_id = _merchant(db_conn)
    order_id = _order(db_conn, merchant_id, 250_000)
    _attempt(db_conn, order_id, 250_000)
    with db_conn.cursor() as cur:
        cur.execute("update payment_attempts set observed_at = now() - interval '2 hours'")
    newest = _attempt(db_conn, order_id, 250_000)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    assert [i.payment_attempt_id for i in result.items] == [newest]


def test_paid_orders_are_never_at_risk_even_with_failed_attempts(db_conn):
    """A later attempt succeeded, so nothing is outstanding. Counting these
    would be the single largest source of denominator inflation, because most
    failed payments on a healthy merchant are eventually paid."""
    merchant_id = _merchant(db_conn)
    order_id = _order(db_conn, merchant_id, 500_000, status="paid", amount_paid=500_000)
    _attempt(db_conn, order_id, 500_000)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    assert result.detected_count == 0
    assert result.revenue_at_risk == 0


def test_each_authorized_uncaptured_payment_counts_separately(db_conn):
    """Unlike failures, each authorisation is separately capturable, so two of
    them on one order really is two lots of money."""
    merchant_id = _merchant(db_conn)
    order_id = _order(db_conn, merchant_id, 300_000)
    _attempt(db_conn, order_id, 150_000, status="authorized")
    _attempt(db_conn, order_id, 150_000, status="authorized")

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    assert result.detected_count == 2
    assert result.revenue_at_risk == 300_000
    assert all(i.risk_reason_codes == ["AUTHORIZED_NOT_CAPTURED"] for i in result.items)


def test_failed_amount_is_the_orders_outstanding_balance_not_the_attempt_amount(db_conn):
    """A partially-paid order has less at risk than its face value. Using the
    attempt's own amount would overstate it."""
    merchant_id = _merchant(db_conn)
    order_id = _order(db_conn, merchant_id, 400_000, amount_paid=150_000)
    _attempt(db_conn, order_id, 400_000)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    assert result.revenue_at_risk == 250_000


def test_the_batch_total_is_a_projection_of_its_own_items(db_conn):
    """The header must be derivable from the detail rather than accumulated
    separately, so the two can never disagree."""
    merchant_id = _merchant(db_conn)
    for amount in (100_000, 250_000, 75_000):
        _attempt(db_conn, _order(db_conn, merchant_id, amount), amount)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    assert result.revenue_at_risk == sum(i.amount_at_risk for i in result.items)
    assert result.detected_count == len(result.items)


def test_items_are_ordered_by_amount_so_a_truncated_run_works_the_biggest_money(db_conn):
    merchant_id = _merchant(db_conn)
    for amount in (50_000, 900_000, 200_000):
        _attempt(db_conn, _order(db_conn, merchant_id, amount), amount)

    result = detect_revenue_at_risk(db_conn, merchant_id, SOURCE_SYNTHETIC)
    amounts = [i.amount_at_risk for i in result.items]
    assert amounts == sorted(amounts, reverse=True)


def test_source_must_be_declared_and_is_rejected_otherwise(db_conn):
    """A batch that does not say whether its money is real cannot be created."""
    merchant_id = _merchant(db_conn)
    with pytest.raises(ValueError, match="must be"):
        detect_revenue_at_risk(db_conn, merchant_id, "definitely_real_money")
