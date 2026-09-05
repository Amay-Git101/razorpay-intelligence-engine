"""Creates real Razorpay Test Mode orders for a guided experiment and
records them as a frozen cohort.

WHAT IS REAL HERE
Every order this module creates is created by calling Razorpay's Orders
API. There is no local fabrication path and no "demo mode" branch: if
Razorpay does not return an order, this function raises and no cohort
row is written for it. The order object persisted as `raw_reference` is
Razorpay's own response verbatim.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
It does not pay for anything. Creating an order is a request-for-payment;
the payment itself can only be completed by a human in Razorpay Checkout,
and no server-side call can stand in for that. This module therefore
cannot manufacture a success or a failure -- the outcomes an experiment
observes are whatever the person at the browser actually produced. That
limitation is the reason the cohort's results are worth anything.

COMMIT-PER-ORDER, ON PURPOSE
Each order is committed as soon as it is persisted. A Razorpay order that
has been created exists whether or not our transaction later succeeds, so
holding all six in one transaction risks the worst outcome: real orders
that exist at Razorpay with no local record of them. Committing as we go
keeps the database aligned with what has actually been created upstream.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from pydantic import BaseModel

from repository.orders import upsert_order
from repository.payment_experiments import (
    get_experiment,
    insert_experiment,
    insert_experiment_order,
    list_experiment_orders_with_state,
)

# A guided experiment is a demonstration, not a load test. Six is the
# largest cohort any journey asks for, and every order in it has to be paid
# by hand in a browser -- so the ceiling is set by what a person can
# actually complete, and it is enforced rather than documented.
MAX_ORDERS_PER_EXPERIMENT = 6

EXPERIMENT_KINDS = ("capture_decision", "failure_pattern", "customer_history")


class CreatedOrder(BaseModel):
    position: int
    order_id: str
    amount: int
    currency: str
    status: str


class ExperimentCreationResult(BaseModel):
    experiment_id: str
    merchant_id: str
    kind: str
    orders: list[CreatedOrder]


class CohortAlreadyInUse(Exception):
    """Raised when orders are added to a cohort that already has an observed
    payment. See _next_position for why that is refused."""


def _next_position(conn: psycopg.Connection, experiment_id: UUID | str) -> int:
    """Where the next order goes, and the guard that keeps a cohort's
    denominator honest.

    Adding orders to a cohort is allowed only while NONE of its orders has
    been paid. Once an outcome exists, the group being judged is fixed:
    appending to it afterwards would let someone who disliked "4 of 6" turn
    it into "4 of 9" by adding three fresh orders, which is the denominator
    manipulation the whole cohort table exists to prevent.

    Creation-time appending is what makes honest progressive creation
    possible -- six separate calls, six real orders, each card appearing
    when its own order actually exists -- without a fake progress bar over
    a single request.
    """
    rows = list_experiment_orders_with_state(conn, experiment_id)
    if any(row["payment_status"] is not None for row in rows):
        raise CohortAlreadyInUse(
            "this group already has a payment result -- orders cannot be added to it now"
        )
    return len(rows) + 1


def create_test_orders(
    conn: psycopg.Connection,
    order_client: Any,
    merchant_id: str,
    kind: str,
    count: int,
    amount: int,
    currency: str = "INR",
    label: str | None = None,
    experiment_id: UUID | str | None = None,
) -> ExperimentCreationResult:
    """Creates `count` real Test Mode orders and freezes them into a cohort.

    `order_client` is injected rather than constructed here so tests can
    supply a double, and so the only place that can build a real
    credentialed client stays the API layer that owns the request.

    Passing `experiment_id` appends to an existing cohort instead of
    starting one, which is how the six-payment experiment creates its
    orders one real call at a time.
    """
    if kind not in EXPERIMENT_KINDS:
        raise ValueError(f"kind must be one of {EXPERIMENT_KINDS}, got {kind!r}")
    if not 1 <= count <= MAX_ORDERS_PER_EXPERIMENT:
        raise ValueError(
            f"count must be between 1 and {MAX_ORDERS_PER_EXPERIMENT}, got {count}"
        )
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    if experiment_id is None:
        experiment_id = insert_experiment(conn, merchant_id, kind, label)
        conn.commit()
        start_position = 1
    else:
        existing = get_experiment(conn, experiment_id)
        if existing is None:
            raise ValueError(f"no experiment with id {experiment_id}")
        if str(existing["merchant_id"]) != str(merchant_id):
            raise ValueError("that experiment belongs to a different merchant")
        start_position = _next_position(conn, experiment_id)
        if start_position + count - 1 > MAX_ORDERS_PER_EXPERIMENT:
            raise ValueError(
                f"a group may hold at most {MAX_ORDERS_PER_EXPERIMENT} orders"
            )

    created: list[CreatedOrder] = []
    for position in range(start_position, start_position + count):
        order = order_client.create_order(
            amount=amount,
            currency=currency,
            receipt=f"exp-{experiment_id}-{position}",
            notes={"experiment_kind": kind, "position": str(position)},
        )

        upsert_order(
            conn,
            order_id=order["id"],
            merchant_id=merchant_id,
            amount=order["amount"],
            amount_paid=order.get("amount_paid", 0),
            amount_due=order.get("amount_due", order["amount"]),
            status=order["status"],
            attempts=order.get("attempts", 0),
            currency=order.get("currency", currency),
            raw_reference=order,
        )
        insert_experiment_order(conn, experiment_id, order["id"], position)
        conn.commit()

        created.append(
            CreatedOrder(
                position=position,
                order_id=order["id"],
                amount=order["amount"],
                currency=order.get("currency", currency),
                status=order["status"],
            )
        )

    return ExperimentCreationResult(
        experiment_id=str(experiment_id),
        merchant_id=merchant_id,
        kind=kind,
        orders=created,
    )
