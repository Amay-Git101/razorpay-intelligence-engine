"""Is this one payment failing, or are many payments failing?

Deterministic aggregation over observed payment outcomes. No model is
used here, and no model could help: how many payments failed is a
counting question, not a judgement.

THE THREE LAYERS ARE KEPT SEPARATE, DELIBERATELY

  ObservedFacts      What is in the database. Counts of rows. Checkable by
                     anyone with SQL access; nothing here is inferred.
  ComputedSignals    Arithmetic on those counts -- a rate, a time span, a
                     concentration share. Still not a claim about the
                     world, just division.
  Interpretation     The only layer that says what it might MEAN, with an
                     explicit list of what it cannot know.

They are separate types rather than one flattened blob because the whole
credibility of this feature rests on a reader being able to check the
interpretation against the facts it came from. A single merged object
would let a confident sentence travel without its evidence.

WHAT THIS CANNOT ESTABLISH, EVER
This system sees one merchant's payments, in Razorpay Test Mode, through
its own database. It cannot see Razorpay's platform health, other
merchants' traffic, or issuer-side status. So it will report that
failures are concentrated, and that a wider problem is *consistent with*
what was observed. It will never report that Razorpay is down, because
nothing available here could establish that. Every report carries that
limitation in `interpretation.limitations` rather than leaving it to be
inferred from a caveat somewhere in the UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

ANALYSIS_VERSION = "failure_pattern_v1"

# Below this many observed payments, no rate is meaningful and the report
# says so instead of dividing by a number too small to divide by.
MIN_OBSERVATIONS_FOR_A_RATE = 3

# A failure rate at or above this, with at least MIN_FAILURES_FOR_PATTERN
# failures, is reported as concentrated. Both are stated as named constants
# rather than inline numbers so the threshold a conclusion depends on is
# visible in the report itself (`thresholds` below) and can be argued with.
CONCENTRATION_RATE_THRESHOLD = 0.5
MIN_FAILURES_FOR_PATTERN = 3

INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
NO_FAILURES = "NO_FAILURES"
ISOLATED_FAILURE = "ISOLATED_FAILURE"
MULTIPLE_FAILURES = "MULTIPLE_FAILURES"
CONCENTRATED_FAILURES = "CONCENTRATED_FAILURES"


class ObservedFacts(BaseModel):
    """Row counts. Nothing derived."""

    payments_observed: int
    captured: int
    authorized_not_captured: int
    failed: int
    other_states: int
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    first_failure_at: str | None = None
    last_failure_at: str | None = None
    failure_reason_counts: dict[str, int] = {}
    orders_without_a_payment_attempt: int = 0


class ComputedSignals(BaseModel):
    """Arithmetic on ObservedFacts. Still not a claim about the world."""

    failure_rate: float | None = None
    failure_window_seconds: float | None = None
    dominant_failure_reason: str | None = None
    dominant_failure_reason_share: float | None = None
    distinct_failure_reasons: int = 0


class Interpretation(BaseModel):
    """The only layer that says what it might mean."""

    code: str
    headline: str
    detail: str
    # True only when the observations are consistent with a problem wider
    # than a single payment. Never asserts a cause.
    consistent_with_wider_problem: bool
    limitations: list[str]


class FailurePatternReport(BaseModel):
    scope: str
    scope_id: str
    analysis_version: str = ANALYSIS_VERSION
    thresholds: dict[str, float]
    observed: ObservedFacts
    computed: ComputedSignals
    interpretation: Interpretation


_ALWAYS_TRUE_LIMITATIONS = [
    "This looks at one merchant's payments in this database only. It cannot "
    "observe Razorpay's platform-wide health, other merchants' traffic, or "
    "issuer-side status.",
    "Payments made in Razorpay Test Mode fail when the payer chooses a "
    "failure method, so a high failure rate here reflects what was tested, "
    "not a live incident.",
]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _observed_from_rows(rows: list[dict[str, Any]], orders_without_attempt: int) -> ObservedFacts:
    captured = sum(1 for r in rows if r["status"] == "captured")
    authorized = sum(1 for r in rows if r["status"] == "authorized" and not r["captured"])
    failed_rows = [r for r in rows if r["status"] == "failed"]
    other = len(rows) - captured - authorized - len(failed_rows)

    observed_times = [r["observed_at"] for r in rows if r["observed_at"] is not None]
    failure_times = [r["observed_at"] for r in failed_rows if r["observed_at"] is not None]

    reason_counts: dict[str, int] = {}
    for row in failed_rows:
        # A failed payment with no error_reason is still a failure; it is
        # bucketed under an explicit label rather than dropped, so the
        # reason counts always sum to the failure count.
        reason = row.get("error_reason") or "unspecified"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return ObservedFacts(
        payments_observed=len(rows),
        captured=captured,
        authorized_not_captured=authorized,
        failed=len(failed_rows),
        other_states=other,
        first_observed_at=_iso(min(observed_times)) if observed_times else None,
        last_observed_at=_iso(max(observed_times)) if observed_times else None,
        first_failure_at=_iso(min(failure_times)) if failure_times else None,
        last_failure_at=_iso(max(failure_times)) if failure_times else None,
        failure_reason_counts=reason_counts,
        orders_without_a_payment_attempt=orders_without_attempt,
    )


def _compute(observed: ObservedFacts, rows: list[dict[str, Any]]) -> ComputedSignals:
    failure_rate = (
        observed.failed / observed.payments_observed
        if observed.payments_observed >= MIN_OBSERVATIONS_FOR_A_RATE
        else None
    )

    window_seconds: float | None = None
    failure_times = [r["observed_at"] for r in rows if r["status"] == "failed" and r["observed_at"]]
    if len(failure_times) >= 2:
        window_seconds = (max(failure_times) - min(failure_times)).total_seconds()

    dominant_reason: str | None = None
    dominant_share: float | None = None
    if observed.failure_reason_counts:
        dominant_reason, dominant_count = max(
            observed.failure_reason_counts.items(), key=lambda kv: kv[1]
        )
        dominant_share = dominant_count / observed.failed if observed.failed else None

    return ComputedSignals(
        failure_rate=failure_rate,
        failure_window_seconds=window_seconds,
        dominant_failure_reason=dominant_reason,
        dominant_failure_reason_share=dominant_share,
        distinct_failure_reasons=len(observed.failure_reason_counts),
    )


def _interpret(observed: ObservedFacts, computed: ComputedSignals) -> Interpretation:
    limitations = list(_ALWAYS_TRUE_LIMITATIONS)

    if observed.payments_observed < MIN_OBSERVATIONS_FOR_A_RATE:
        return Interpretation(
            code=INSUFFICIENT_DATA,
            headline="Not enough payments observed yet.",
            detail=(
                f"{observed.payments_observed} payment(s) observed. At least "
                f"{MIN_OBSERVATIONS_FOR_A_RATE} are needed before a failure rate says anything."
            ),
            consistent_with_wider_problem=False,
            limitations=limitations,
        )

    if observed.failed == 0:
        return Interpretation(
            code=NO_FAILURES,
            headline="No failures observed.",
            detail=f"All {observed.payments_observed} observed payments are in a non-failed state.",
            consistent_with_wider_problem=False,
            limitations=limitations,
        )

    if observed.failed == 1:
        return Interpretation(
            code=ISOLATED_FAILURE,
            headline="This looks isolated.",
            detail=(
                f"1 of {observed.payments_observed} observed payments failed. "
                "A single failure is what an ordinary declined payment looks like."
            ),
            consistent_with_wider_problem=False,
            limitations=limitations,
        )

    rate = computed.failure_rate or 0.0
    concentrated = rate >= CONCENTRATION_RATE_THRESHOLD and observed.failed >= MIN_FAILURES_FOR_PATTERN

    if concentrated:
        detail = (
            f"{observed.failed} of {observed.payments_observed} observed payments failed "
            f"({rate:.0%}), which is at or above the {CONCENTRATION_RATE_THRESHOLD:.0%} threshold."
        )
        if computed.dominant_failure_reason and (computed.dominant_failure_reason_share or 0) >= 0.5:
            detail += (
                f" {computed.dominant_failure_reason_share:.0%} of those failures share the same "
                f"reported reason ({computed.dominant_failure_reason})."
            )
        return Interpretation(
            code=CONCENTRATED_FAILURES,
            headline="Failures are concentrated — this is more than one isolated payment.",
            detail=detail,
            consistent_with_wider_problem=True,
            limitations=limitations
            + [
                "Concentration is consistent with a wider problem. It does not "
                "identify the cause, and it is not evidence that the gateway itself is down."
            ],
        )

    # Two separate conditions have to hold for "concentrated", and the
    # detail must name the one that actually failed. Reporting the rate as
    # the reason when the rate is fine (2 of 4 is exactly at the threshold,
    # not below it) would be a false statement about the data, in the one
    # sentence a reader is most likely to quote.
    if rate < CONCENTRATION_RATE_THRESHOLD:
        reason = (
            f"a {rate:.0%} failure rate, below the {CONCENTRATION_RATE_THRESHOLD:.0%} threshold"
        )
    else:
        reason = (
            f"{observed.failed} failures, fewer than the {MIN_FAILURES_FOR_PATTERN} needed to call a "
            f"pattern -- the {rate:.0%} rate alone is over a small number of payments"
        )

    return Interpretation(
        code=MULTIPLE_FAILURES,
        headline="More than one payment failed, but the pattern is not concentrated.",
        detail=(
            f"{observed.failed} of {observed.payments_observed} observed payments failed: {reason}."
        ),
        consistent_with_wider_problem=False,
        limitations=limitations,
    )


def _build(scope: str, scope_id: str, rows: list[dict[str, Any]], orders_without_attempt: int) -> FailurePatternReport:
    observed = _observed_from_rows(rows, orders_without_attempt)
    computed = _compute(observed, rows)
    return FailurePatternReport(
        scope=scope,
        scope_id=scope_id,
        thresholds={
            "min_observations_for_a_rate": float(MIN_OBSERVATIONS_FOR_A_RATE),
            "concentration_rate_threshold": CONCENTRATION_RATE_THRESHOLD,
            "min_failures_for_pattern": float(MIN_FAILURES_FOR_PATTERN),
        },
        observed=observed,
        computed=computed,
        interpretation=_interpret(observed, computed),
    )


# The most recent payment attempts for one merchant. Ordered by observation
# time and capped, so "recent" is a defined set rather than the whole table.
_RECENT_SQL = """
select p.id, p.status, p.captured, p.error_reason, p.observed_at
  from payment_attempts p
  join orders o on p.order_id = o.id
 where o.merchant_id = %s
 order by p.observed_at desc, p.id desc
 limit %s
"""


def analyze_recent_payments(
    conn: psycopg.Connection, merchant_id: str, limit: int = 20
) -> FailurePatternReport:
    """Problem 02: across this merchant's recent payments, is failure
    activity unusual?"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_RECENT_SQL, (merchant_id, limit))
        rows = cur.fetchall()
    return _build("merchant_recent_payments", str(merchant_id), rows, orders_without_attempt=0)


# One row per order in the cohort, with its latest payment attempt if any.
# Orders nobody has paid yet come back with a null status and are counted
# separately rather than dropped -- see the note in
# repository/payment_experiments.py about not letting the denominator shrink.
_COHORT_SQL = """
select
    latest.id           as id,
    latest.status       as status,
    latest.captured     as captured,
    latest.error_reason as error_reason,
    latest.observed_at  as observed_at
from payment_experiment_orders peo
left join lateral (
    select p.*
      from payment_attempts p
     where p.order_id = peo.order_id
     order by p.observed_at desc, p.id desc
     limit 1
) latest on true
where peo.experiment_id = %s
order by peo.position
"""


def analyze_experiment(
    conn: psycopg.Connection, experiment_id: UUID | str
) -> FailurePatternReport:
    """Problem 03: across one frozen cohort of orders, is this one payment
    failing or many?

    The cohort is fixed at creation time, so the denominator cannot drift
    as outcomes arrive.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COHORT_SQL, (experiment_id,))
        all_rows = cur.fetchall()

    paid_rows = [r for r in all_rows if r["status"] is not None]
    unpaid = len(all_rows) - len(paid_rows)
    return _build("experiment_cohort", str(experiment_id), paid_rows, orders_without_attempt=unpaid)
