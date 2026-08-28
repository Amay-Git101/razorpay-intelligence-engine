"""Reconciliation: the only mechanism by which this project's internal
state is updated from Razorpay in v1 (no webhook ingestion). Callable
on-demand right after a Checkout completion, or later by a periodic
sweep -- both would just call reconcile_order().

Enforces, at the orchestration layer:
  - order-level aggregate state is refreshed wholesale from the fetched
    Order representation, never inferred from payment_attempts rows
    (Phase 2 Rev 2, "order state is a separate aggregate");
  - a new pay_ id always becomes a new payment_attempts row;
  - an existing pay_ id with an unchanged status is a strict no-op;
  - an existing pay_ id with a changed status goes through the DB-backed
    transition guard (repository.payment_attempts); an invalid
    transition never mutates the row -- it produces a
    payment.attempt.anomaly canonical event + RECONCILIATION_ANOMALY
    audit entry instead, and reconciliation continues normally.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from domain.contracts import EntityType, EventSource, EventType
from razorpay_client.client import RazorpayReadClientProtocol
from repository.audit import insert_audit_entry
from repository.canonical_events import insert_canonical_event
from repository.orders import get_order, upsert_order
from repository.payment_attempts import (
    InvalidPaymentAttemptTransition,
    get_payment_attempt,
    insert_payment_attempt,
    update_payment_attempt_status,
)

from .event_mapping import PAYMENT_STATUS_TO_EVENT_TYPE


def reconcile_order(
    conn: psycopg.Connection,
    client: RazorpayReadClientProtocol,
    merchant_id: str,
    order_id: str,
) -> list[UUID]:
    """Runs as a single atomic unit (savepoint if already inside a
    transaction, real transaction otherwise): an unexpected DB error
    rolls back the whole pass, leaving nothing partially persisted. The
    deliberate invalid-transition anomaly path is NOT treated as such an
    error -- it's caught inline and recorded as data.

    Returns the ids of newly created canonical_events. Idempotent: a
    second call with unchanged upstream data returns an empty list and
    creates no new rows.
    """
    new_event_ids: list[UUID] = []
    with conn.transaction():
        new_event_ids.extend(_reconcile_order_aggregate(conn, client, merchant_id, order_id))
        new_event_ids.extend(_reconcile_payment_attempts(conn, client, merchant_id, order_id))
    return new_event_ids


def _reconcile_order_aggregate(
    conn: psycopg.Connection, client: RazorpayReadClientProtocol, merchant_id: str, order_id: str
) -> list[UUID]:
    fetched = client.get_order(order_id)
    existing = get_order(conn, order_id)

    is_first_observation = existing is None
    was_paid = existing is not None and existing["status"] == "paid"
    now_paid = fetched["status"] == "paid"

    # Order aggregate fields (amount_paid, amount_due, status, attempts)
    # come straight from the fetched Order representation -- never
    # derived by counting/inspecting payment_attempts rows.
    upsert_order(
        conn,
        order_id=fetched["id"],
        merchant_id=merchant_id,
        amount=fetched["amount"],
        amount_paid=fetched["amount_paid"],
        amount_due=fetched["amount_due"],
        status=fetched["status"],
        attempts=fetched["attempts"],
        currency=fetched.get("currency", "INR"),
        raw_reference=fetched,
    )

    new_events: list[UUID] = []

    if is_first_observation:
        new_events.append(
            _record_event(conn, merchant_id, EventType.ORDER_CREATED, EntityType.ORDER, order_id, order_id, fetched)
        )

    if now_paid and not was_paid:
        new_events.append(
            _record_event(conn, merchant_id, EventType.ORDER_PAID, EntityType.ORDER, order_id, order_id, fetched)
        )

    return new_events


def _reconcile_payment_attempts(
    conn: psycopg.Connection, client: RazorpayReadClientProtocol, merchant_id: str, order_id: str
) -> list[UUID]:
    new_events: list[UUID] = []

    for payment in client.get_order_payments(order_id):
        payment_id = payment["id"]
        fetched_status = payment["status"]
        existing = get_payment_attempt(conn, payment_id)

        if existing is None:
            insert_payment_attempt(
                conn,
                payment_attempt_id=payment_id,
                order_id=order_id,
                status=fetched_status,
                method=payment.get("method"),
                captured=bool(payment.get("captured", False)),
                error_source=payment.get("error_source"),
                error_step=payment.get("error_step"),
                error_reason=payment.get("error_reason"),
                amount=payment["amount"],
                raw_reference=payment,
            )
            event_type = PAYMENT_STATUS_TO_EVENT_TYPE.get(fetched_status)
            if event_type is not None:
                new_events.append(
                    _record_event(conn, merchant_id, event_type, EntityType.PAYMENT, payment_id, order_id, payment)
                )
            continue

        if existing["status"] == fetched_status:
            # Same id, unchanged status -- strict no-op. No row write,
            # no event, no audit entry.
            continue

        try:
            update_payment_attempt_status(
                conn, payment_id, fetched_status, bool(payment.get("captured", False)), payment
            )
        except InvalidPaymentAttemptTransition:
            anomaly_payload = {
                "payment_id": payment_id,
                "known_status": existing["status"],
                "fetched_status": fetched_status,
            }
            event_id = insert_canonical_event(
                conn, merchant_id, EventType.PAYMENT_ATTEMPT_ANOMALY.value, EventSource.RAZORPAY_API_POLL.value,
                EntityType.PAYMENT.value, payment_id, order_id, datetime.now(timezone.utc), anomaly_payload,
            )
            insert_audit_entry(conn, "RECONCILIATION_ANOMALY", anomaly_payload, event_id=str(event_id))
            new_events.append(event_id)
            continue

        event_type = PAYMENT_STATUS_TO_EVENT_TYPE.get(fetched_status)
        if event_type is not None:
            new_events.append(
                _record_event(conn, merchant_id, event_type, EntityType.PAYMENT, payment_id, order_id, payment)
            )

    return new_events


def _record_event(
    conn: psycopg.Connection,
    merchant_id: str,
    event_type: EventType,
    entity_type: EntityType,
    entity_id: str,
    order_id: str,
    payload: dict[str, Any],
) -> UUID:
    event_id = insert_canonical_event(
        conn, merchant_id, event_type.value, EventSource.RAZORPAY_API_POLL.value,
        entity_type.value, entity_id, order_id, datetime.now(timezone.utc), payload,
    )
    insert_audit_entry(
        conn, "EVENT_INGESTED", {"event_type": event_type.value, "entity_id": entity_id}, event_id=str(event_id)
    )
    return event_id
