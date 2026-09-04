"""Revenue-at-risk detection: the stage this project previously had no
equivalent of at all.

Before this module, nothing in the system could answer "what revenue is at
risk right now?". The pipeline could only be pointed at an order id somebody
already knew about. Detection is what turns that into an agent that finds
the money itself, and it is the first of the Buildathon track's three
requirements ("detects revenue at risk").

DELIBERATELY DETERMINISTIC -- NO MODEL IS USED HERE. Whether revenue is at
risk is a question about observed payment state, and observed payment state
is a fact, not a judgement: an unpaid order with a failed attempt has money
at risk whatever any model thinks. The AI layer is used one stage later, for
the genuinely uncertain question of WHY it failed and what to do about it.
Using a model for detection would add cost and a failure mode without adding
information.

TWO RISK CLASSES ARE DETECTED

  FAILED_PAYMENT_ORDER_UNPAID
      The customer tried to pay and the attempt failed, and the order has
      still not been paid by any other attempt. This is the backlog the
      whole product is about, and it is the class Razorpay itself will tell
      you about but will not act on.

  AUTHORIZED_NOT_CAPTURED
      The money is authorised but has not been taken. Genuinely at risk
      because an authorisation expires.

WHAT IS EXCLUDED, AND WHY IT MATTERS FOR THE NUMBERS
  - Orders already in status 'paid' -- some later attempt succeeded, so
    nothing is at risk even though an earlier attempt failed. Counting these
    would inflate "revenue at risk", which is the denominator of every
    recovery percentage this system reports. Inflating a denominator to make
    a recovery rate look better is exactly the dishonesty this codebase's
    metrics discipline exists to prevent -- so the exclusion is enforced in
    SQL here, not left to the caller.
  - Payment attempts that already have an action recorded against them --
    they have been through recovery once already and are not fresh risk.

The amount at risk for a failed attempt is the ORDER's outstanding
amount_due, not the failed attempt's own amount: what is at risk is the
revenue the merchant has not collected, which is an order-level fact. For an
authorised-not-captured attempt it is the attempt's own amount, which is the
sum actually authorised and capturable.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from repository.recovery_batches import finalize_batch, insert_batch, insert_batch_item

DETECTION_VERSION = "detection_v1"

SOURCE_RAZORPAY_TEST_MODE = "razorpay_test_mode"
SOURCE_SYNTHETIC = "synthetic"


class RiskItem(BaseModel):
    order_id: str
    payment_attempt_id: str
    amount_at_risk: int
    risk_reason_codes: list[str]
    prior_attempt_count: int


class RiskDetectionResult(BaseModel):
    batch_id: str
    merchant_id: str
    source: str
    detected_count: int
    revenue_at_risk: int
    items: list[RiskItem]


# One statement, so the at-risk set is defined in exactly one place rather
# than assembled across several queries that could drift apart.
#
# prior_attempt_count counts the order's OTHER failed attempts, excluding the
# row itself. It is the input to the retry-budget stopping rule, so it must
# count what has already been tried, not what is being considered now.
_DETECT_SQL = """
with eligible as (
    select p.*, o.amount_due
    from payment_attempts p
    join orders o on p.order_id = o.id
    where o.merchant_id = %s
      and o.status <> 'paid'
      and not exists (
            select 1
              from actions a
              join decisions d on a.decision_id = d.id
             where d.payment_attempt_id = p.id
          )
),
-- AT MOST ONE ROW PER ORDER for failed payments. This is the anti-double-count
-- rule and it is the reason the query is shaped this way rather than as a flat
-- select.
--
-- What is at risk on a failed payment is the ORDER's outstanding amount_due --
-- an order-level fact. An order the customer retried three times has failed
-- three times but still has exactly one outstanding balance. Emitting a row
-- per attempt would count that balance three times and inflate
-- revenue_at_risk, which is the denominator of every recovery percentage this
-- system reports. Inflating a denominator to flatter a recovery rate is
-- precisely the dishonesty the metrics discipline here exists to prevent, so
-- the constraint lives in SQL rather than in a caller's good intentions.
--
-- The latest attempt is the one kept: it carries the most recent failure
-- evidence, which is what the diagnosis should be based on.
failed_latest as (
    select distinct on (order_id)
        id         as payment_attempt_id,
        order_id   as order_id,
        status     as attempt_status,
        amount_due as amount_at_risk
    from eligible
    where status = 'failed'
    order by order_id, observed_at desc, id desc
),
-- One row per authorized-but-uncaptured PAYMENT, by contrast: each
-- authorisation is separately capturable, so each is genuinely separate money.
-- The amount is the authorisation's own, not the order's balance.
authorized_uncaptured as (
    select
        id       as payment_attempt_id,
        order_id as order_id,
        status   as attempt_status,
        amount   as amount_at_risk
    from eligible
    where status = 'authorized' and captured = false
),
combined as (
    select * from failed_latest
    union all
    select * from authorized_uncaptured
)
select
    c.payment_attempt_id,
    c.order_id,
    c.attempt_status,
    c.amount_at_risk,
    -- The order's OTHER failed attempts, excluding this row. This is the
    -- number the retry-budget stopping rule reads: what has already been
    -- tried, not what is being considered now.
    (
        select count(*)
          from payment_attempts sibling
         where sibling.order_id = c.order_id
           and sibling.status = 'failed'
           and sibling.id <> c.payment_attempt_id
    ) as prior_attempt_count
from combined c
order by c.amount_at_risk desc
"""


def _reason_codes(attempt_status: str) -> list[str]:
    if attempt_status == "failed":
        return ["FAILED_PAYMENT_ORDER_UNPAID"]
    return ["AUTHORIZED_NOT_CAPTURED"]


def detect_revenue_at_risk(
    conn: psycopg.Connection, merchant_id: str, source: str
) -> RiskDetectionResult:
    """Scans one merchant's payments, records a batch, and returns it.

    `source` is required and has no default: the caller must state whether
    this batch's money is real Razorpay Test Mode money or synthetic. The
    database CHECK constraint rejects anything else. There is deliberately no
    way to create a batch without answering that question.
    """
    if source not in (SOURCE_RAZORPAY_TEST_MODE, SOURCE_SYNTHETIC):
        raise ValueError(
            f"source must be {SOURCE_RAZORPAY_TEST_MODE!r} or {SOURCE_SYNTHETIC!r}, got {source!r} -- "
            "a batch must declare whether its money is real"
        )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DETECT_SQL, (merchant_id,))
        rows: list[dict[str, Any]] = cur.fetchall()

    batch_id: UUID = insert_batch(conn, merchant_id, source, DETECTION_VERSION)

    items: list[RiskItem] = []
    for row in rows:
        reason_codes = _reason_codes(row["attempt_status"])
        insert_batch_item(
            conn,
            batch_id,
            row["order_id"],
            row["payment_attempt_id"],
            row["amount_at_risk"],
            reason_codes,
        )
        items.append(
            RiskItem(
                order_id=row["order_id"],
                payment_attempt_id=row["payment_attempt_id"],
                amount_at_risk=row["amount_at_risk"],
                risk_reason_codes=reason_codes,
                prior_attempt_count=row["prior_attempt_count"],
            )
        )

    batch = finalize_batch(conn, batch_id)
    return RiskDetectionResult(
        batch_id=str(batch_id),
        merchant_id=merchant_id,
        source=source,
        detected_count=batch["detected_count"],
        revenue_at_risk=batch["revenue_at_risk"],
        items=items,
    )
