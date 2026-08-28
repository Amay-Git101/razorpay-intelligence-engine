"""Razorpay payment status -> canonical EventType mapping.

"created" is intentionally absent: it is DOCUMENTED only (Phase 1 never
independently observed a payment object sitting in `created`). If a
fetched payment status doesn't appear in this map, the caller in
service.py treats it as "no event type for this observation" rather than
guessing -- it still updates/inserts the payment_attempts row, but does
not fabricate a canonical event for an unrecognized/unverified status.
"""

from __future__ import annotations

from domain.contracts import EventType

PAYMENT_STATUS_TO_EVENT_TYPE: dict[str, EventType] = {
    "failed": EventType.PAYMENT_ATTEMPT_FAILED,
    "authorized": EventType.PAYMENT_ATTEMPT_AUTHORIZED,
    "captured": EventType.PAYMENT_ATTEMPT_CAPTURED,
}
