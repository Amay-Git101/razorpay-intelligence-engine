"""Builds a ContextSnapshot from a canonical_events row.

Explicitly event-type aware (per Phase 3 gate review): a
payment-attempt-scoped event (payment.attempt.failed/authorized/
captured/anomaly) populates payment fields plus the DERIVED
attempt_number; an order-level event (order.created/order.paid) is never
forced into a payment-attempt shape -- it gets order aggregate fields
only, and payment_attempt_id stays None.

A payload missing a field required for its event category raises
ContextBuildError. It never falls back to None or a fabricated value --
a malformed/incomplete observation must be visibly rejected, not quietly
smoothed over.
"""

from __future__ import annotations

from typing import Any

import psycopg

from domain.contracts import ContextSnapshot, EventType, ProvenanceBand, ProvenancedField
from repository.payment_attempts import list_payment_attempts_for_order

_PAYMENT_EVENT_TYPES = {
    EventType.PAYMENT_ATTEMPT_FAILED,
    EventType.PAYMENT_ATTEMPT_AUTHORIZED,
    EventType.PAYMENT_ATTEMPT_CAPTURED,
    EventType.PAYMENT_ATTEMPT_ANOMALY,
}
_ORDER_EVENT_TYPES = {
    EventType.ORDER_CREATED,
    EventType.ORDER_PAID,
}

_ORDER_REQUIRED_FIELDS = ("amount", "amount_paid", "amount_due", "status", "attempts")


class ContextBuildError(Exception):
    """Raised when an event's payload is missing a field required for
    its event_type. Fails loudly rather than substituting None or a
    fabricated value."""


def build_context_snapshot(conn: psycopg.Connection, event: dict[str, Any]) -> ContextSnapshot:
    event_type = EventType(event["event_type"])
    payload = event["payload"]
    order_id = event["order_id"]

    if event_type in _PAYMENT_EVENT_TYPES:
        return _build_payment_attempt_context(conn, event, payload, order_id)
    if event_type in _ORDER_EVENT_TYPES:
        return _build_order_context(payload, order_id)

    raise ContextBuildError(f"unsupported event_type for context building: {event_type.value}")


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] is None:
        raise ContextBuildError(f"payload missing required field '{key}'")
    return payload[key]


def _build_payment_attempt_context(
    conn: psycopg.Connection, event: dict[str, Any], payload: dict[str, Any], order_id: str
) -> ContextSnapshot:
    payment_attempt_id = event["entity_id"]
    amount = _require(payload, "amount")

    fields = [
        ProvenancedField(field="amount", value=amount, band=ProvenanceBand.RAW, source="razorpay_api_poll"),
    ]

    # method is NOT required: only 'card' is VERIFIED, and even for card
    # payments the field's guaranteed presence on every payload shape
    # hasn't been independently proven -- omit rather than fabricate.
    if payload.get("method") is not None:
        fields.append(
            ProvenancedField(field="method", value=payload["method"], band=ProvenanceBand.RAW, source="razorpay_api_poll")
        )

    # error_source/step/reason are only present on failed attempts --
    # optional by nature of the event, not a missing-data problem.
    for key in ("error_source", "error_step", "error_reason"):
        if payload.get(key) is not None:
            fields.append(
                ProvenancedField(field=key, value=payload[key], band=ProvenanceBand.RAW, source="razorpay_api_poll")
            )

    attempt_number = len(list_payment_attempts_for_order(conn, order_id))
    fields.append(
        ProvenancedField(field="attempt_number", value=attempt_number, band=ProvenanceBand.DERIVED, source="internal_count")
    )

    return ContextSnapshot(order_id=order_id, payment_attempt_id=payment_attempt_id, fields=fields)


def _build_order_context(payload: dict[str, Any], order_id: str) -> ContextSnapshot:
    fields = [
        ProvenancedField(field=key, value=_require(payload, key), band=ProvenanceBand.RAW, source="razorpay_api_poll")
        for key in _ORDER_REQUIRED_FIELDS
    ]
    return ContextSnapshot(order_id=order_id, payment_attempt_id=None, fields=fields)
