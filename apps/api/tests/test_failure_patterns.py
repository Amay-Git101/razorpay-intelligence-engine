"""Failure-pattern analysis, against a real database.

Two things are being defended here.

The first is the DENOMINATOR, again. `analyze_experiment` reports "N of M
failed", and M is the whole reason the conclusion means anything. An order
in the cohort that nobody has paid yet must not quietly disappear from M,
because that would turn "4 of 6" into "4 of 4" and flip a measured
conclusion into a much stronger one that the data does not support.

The second is that the module NEVER OVERCLAIMS. It is allowed to say
failures are concentrated. It is not allowed to say the gateway is down,
because nothing it can observe could establish that.
"""

from __future__ import annotations

import uuid

import pytest

from repository.merchants import insert_merchant
from repository.orders import upsert_order
from repository.payment_attempts import insert_payment_attempt
from repository.payment_experiments import insert_experiment, insert_experiment_order
from risk.failure_patterns import (
    CONCENTRATED_FAILURES,
    INSUFFICIENT_DATA,
    ISOLATED_FAILURE,
    MULTIPLE_FAILURES,
    NO_FAILURES,
    analyze_experiment,
    analyze_recent_payments,
)


def _merchant(db_conn) -> str:
    return str(insert_merchant(db_conn, "Pattern Test Merchant", {}, {}))


def _order(db_conn, merchant_id: str, amount: int = 50000, status: str = "attempted") -> str:
    order_id = f"order_PAT_{uuid.uuid4().hex[:10]}"
    upsert_order(
        db_conn, order_id=order_id, merchant_id=merchant_id, amount=amount,
        amount_paid=0, amount_due=amount, status=status, attempts=1,
        currency="INR", raw_reference={"test": True},
    )
    return order_id


def _attempt(
    db_conn,
    order_id: str,
    status: str = "failed",
    captured: bool = False,
    error_reason: str | None = "payment_failed",
    amount: int = 50000,
) -> str:
    payment_id = f"pay_PAT_{uuid.uuid4().hex[:10]}"
    insert_payment_attempt(
        db_conn, payment_attempt_id=payment_id, order_id=order_id, status=status,
        method="card", captured=captured,
        error_source="bank" if status == "failed" else None,
        error_step="payment_authorization" if status == "failed" else None,
        error_reason=error_reason if status == "failed" else None,
        amount=amount, raw_reference={"test": True},
    )
    return payment_id


# ---------------------------------------------------------------------------
# Interpretation follows the observations, in both directions
# ---------------------------------------------------------------------------


def test_four_of_six_failed_is_reported_as_concentrated(db_conn):
    merchant_id = _merchant(db_conn)
    for _ in range(4):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    for _ in range(2):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.observed.payments_observed == 6
    assert report.observed.failed == 4
    assert report.computed.failure_rate == pytest.approx(4 / 6)
    assert report.interpretation.code == CONCENTRATED_FAILURES
    assert report.interpretation.consistent_with_wider_problem is True


def test_one_of_six_failed_is_reported_as_isolated(db_conn):
    """The same code path, opposite data, opposite conclusion -- the
    conclusion is driven by the observations, not by the scenario."""
    merchant_id = _merchant(db_conn)
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    for _ in range(5):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.observed.failed == 1
    assert report.interpretation.code == ISOLATED_FAILURE
    assert report.interpretation.consistent_with_wider_problem is False


def test_no_failures_is_reported_as_no_failures(db_conn):
    merchant_id = _merchant(db_conn)
    for _ in range(4):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.interpretation.code == NO_FAILURES
    assert report.interpretation.consistent_with_wider_problem is False


def test_two_failures_below_the_threshold_is_not_called_concentrated(db_conn):
    """2 of 8 is 25%: more than one failure, but not a pattern. The middle
    case exists so the module is not a two-state 'fine / catastrophe' switch."""
    merchant_id = _merchant(db_conn)
    for _ in range(2):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    for _ in range(6):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.observed.failed == 2
    assert report.interpretation.code == MULTIPLE_FAILURES
    assert report.interpretation.consistent_with_wider_problem is False


def test_a_rate_at_the_threshold_is_not_described_as_below_it(db_conn):
    """Regression. 2 of 4 is exactly at the 50% threshold, so the reason it
    is not called concentrated is the failure COUNT, not the rate. Saying
    'below the threshold' here would be a false statement about the data in
    the sentence a reader is most likely to quote."""
    merchant_id = _merchant(db_conn)
    for _ in range(2):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    for _ in range(2):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.computed.failure_rate == pytest.approx(0.5)
    assert report.interpretation.code == MULTIPLE_FAILURES
    assert "below" not in report.interpretation.detail
    assert "fewer than" in report.interpretation.detail


def test_a_rate_genuinely_below_the_threshold_says_so(db_conn):
    merchant_id = _merchant(db_conn)
    for _ in range(2):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    for _ in range(6):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.interpretation.code == MULTIPLE_FAILURES
    assert "below" in report.interpretation.detail


def test_too_few_payments_reports_insufficient_data_rather_than_a_rate(db_conn):
    merchant_id = _merchant(db_conn)
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.interpretation.code == INSUFFICIENT_DATA
    assert report.computed.failure_rate is None, "a rate over 1 observation must not be reported"


# ---------------------------------------------------------------------------
# The denominator
# ---------------------------------------------------------------------------


def test_unpaid_cohort_orders_do_not_shrink_the_denominator(db_conn):
    """Four failures in a cohort of six where two were never paid is 4 of 6,
    not 4 of 4. Dropping the unpaid orders would report a 100% failure rate
    for a cohort that is 67% failed."""
    merchant_id = _merchant(db_conn)
    experiment_id = insert_experiment(db_conn, merchant_id, "failure_pattern")

    for position in range(1, 5):
        order_id = _order(db_conn, merchant_id)
        _attempt(db_conn, order_id, status="failed")
        insert_experiment_order(db_conn, experiment_id, order_id, position)

    for position in (5, 6):
        insert_experiment_order(db_conn, experiment_id, _order(db_conn, merchant_id, status="created"), position)

    report = analyze_experiment(db_conn, experiment_id)

    assert report.observed.failed == 4
    assert report.observed.orders_without_a_payment_attempt == 2
    assert report.observed.payments_observed == 4
    assert "4" in report.interpretation.detail


def test_cohort_analysis_is_scoped_to_its_own_orders(db_conn):
    """A cohort's conclusion must not move because unrelated payments exist
    for the same merchant."""
    merchant_id = _merchant(db_conn)
    experiment_id = insert_experiment(db_conn, merchant_id, "failure_pattern")

    for position in range(1, 5):
        order_id = _order(db_conn, merchant_id)
        _attempt(db_conn, order_id, status="captured", captured=True)
        insert_experiment_order(db_conn, experiment_id, order_id, position)

    for _ in range(10):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    report = analyze_experiment(db_conn, experiment_id)

    assert report.observed.payments_observed == 4
    assert report.observed.failed == 0
    assert report.interpretation.code == NO_FAILURES


def test_failure_reason_counts_sum_to_the_failure_count(db_conn):
    """Including failures Razorpay reported no reason for -- they are
    bucketed, never dropped, so the breakdown always reconciles."""
    merchant_id = _merchant(db_conn)
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed", error_reason="payment_failed")
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed", error_reason="payment_failed")
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed", error_reason=None)

    report = analyze_recent_payments(db_conn, merchant_id)

    assert sum(report.observed.failure_reason_counts.values()) == report.observed.failed
    assert "unspecified" in report.observed.failure_reason_counts


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


def test_no_interpretation_ever_claims_the_gateway_is_down(db_conn):
    """The strongest conclusion available is 'consistent with a wider
    problem'. Anything stronger would be unsupportable from one merchant's
    own payment rows."""
    merchant_id = _merchant(db_conn)
    for _ in range(6):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    report = analyze_recent_payments(db_conn, merchant_id)
    spoken = f"{report.interpretation.headline} {report.interpretation.detail}".lower()

    assert report.interpretation.code == CONCENTRATED_FAILURES
    for overclaim in ("razorpay is down", "gateway is down", "outage", "razorpay outage"):
        assert overclaim not in spoken


def test_every_report_carries_its_limitations(db_conn):
    merchant_id = _merchant(db_conn)
    for _ in range(4):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.interpretation.limitations, "a conclusion must never travel without its caveats"
    assert any("platform-wide" in lim or "cannot observe" in lim for lim in report.interpretation.limitations)


def test_thresholds_a_conclusion_depends_on_are_reported_with_it(db_conn):
    """A reader has to be able to disagree with the threshold, which means
    seeing it."""
    merchant_id = _merchant(db_conn)
    for _ in range(4):
        _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    report = analyze_recent_payments(db_conn, merchant_id)

    assert report.thresholds["concentration_rate_threshold"] == 0.5
    assert report.thresholds["min_observations_for_a_rate"] == 3.0
