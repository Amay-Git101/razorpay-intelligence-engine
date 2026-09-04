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
from repository.payment_experiments import insert_experiment, insert_experiment_order

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


def create_test_orders(
    conn: psycopg.Connection,
    order_client: Any,
    merchant_id: str,
    kind: str,
    count: int,
    amount: int,
    currency: str = "INR",
    label: str | None = None,
) -> ExperimentCreationResult:
    """Creates `count` real Test Mode orders and freezes them into a cohort.

    `order_client` is injected rather than constructed here so tests can
    supply a double, and so the only place that can build a real
    credentialed client stays the API layer that owns the request.
    """
    if kind not in EXPERIMENT_KINDS:
        raise ValueError(f"kind must be one of {EXPERIMENT_KINDS}, got {kind!r}")
    if not 1 <= count <= MAX_ORDERS_PER_EXPERIMENT:
        raise ValueError(
            f"count must be between 1 and {MAX_ORDERS_PER_EXPERIMENT}, got {count}"
        )
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")

    experiment_id: UUID = insert_experiment(conn, merchant_id, kind, label)
    conn.commit()

    created: list[CreatedOrder] = []
    for position in range(1, count + 1):
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
