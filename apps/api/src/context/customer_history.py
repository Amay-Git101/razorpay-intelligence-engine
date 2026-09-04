"""Does the customer's previous payment behaviour change the decision?

This module answers the first half of that question -- what the previous
behaviour actually WAS -- from real stored payment records. It makes no
recommendation; intelligence/recovery_engine.py decides what, if
anything, the history changes.

WHERE THE IDENTITY COMES FROM, AND WHY IT IS REAL
Razorpay's payment object carries the payer's `email` and `contact`, and
this system already stores that object verbatim in
`payment_attempts.raw_reference` (it has since migration 0001). So a
customer's prior payments are recoverable from data the system was
already keeping -- nothing is invented here, and no new collection was
added to make this feature work. A payment created by the seeder has no
email or contact, so synthetic rows simply have no identity and no
history, which is the correct answer for them rather than a fabricated
one.

WHAT IS DELIBERATELY NOT DONE
No profile, no score, no label attached to a person. The output is a
count of observable payment outcomes over a bounded lookback, and it is
keyed on a fingerprint rather than the address itself:
`decisions.context_snapshot` is persisted and widely read, and there is
no reason for a customer's email to be copied into it when a stable
opaque key answers the same question. The raw identity stays where it
already was.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

# How far back a customer's history is considered. A payment from two years
# ago says very little about an instrument today, and an unbounded lookback
# would make the signal drift silently as the table grows.
DEFAULT_LOOKBACK_DAYS = 180


class CustomerIdentity(BaseModel):
    """How this payer was recognised. `fingerprint` is what gets persisted."""

    kind: str  # "email" | "contact"
    fingerprint: str


class CustomerHistory(BaseModel):
    identity_kind: str
    identity_fingerprint: str
    lookback_days: int
    prior_payment_count: int
    prior_captured_count: int
    prior_authorized_count: int
    prior_failed_count: int
    distinct_prior_orders: int
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    prior_failure_reasons: dict[str, int] = {}


def _fingerprint(kind: str, value: str) -> str:
    """Stable, opaque, and not reversible to the address by reading the
    database. Salted with the field name so an email and a phone number
    that happened to be identical strings would not collide."""
    digest = hashlib.sha256(f"{kind}:{value.strip().lower()}".encode("utf-8")).hexdigest()
    return digest[:32]


def extract_customer_identity(raw_reference: dict[str, Any] | None) -> CustomerIdentity | None:
    """Reads the payer identity out of a stored Razorpay payment object.

    Email is preferred over contact only because it is the more stable of
    the two in Razorpay Test Mode; either alone is enough. Returns None
    when the payment carries neither, which is the honest result for a
    synthetic row rather than an error.
    """
    if not raw_reference:
        return None
    for kind in ("email", "contact"):
        value = raw_reference.get(kind)
        if isinstance(value, str) and value.strip():
            return CustomerIdentity(kind=kind, fingerprint=_fingerprint(kind, value))
    return None


# Prior payments by the same payer for the same merchant.
#
# "Prior" is enforced two ways: the current payment's own id is excluded, and
# only attempts observed at or before it are counted. Without the second
# condition a payment made after the one being judged would leak into its
# history, which would make the same decision produce different reason codes
# when replayed later.
_HISTORY_SQL = """
select
    p.id,
    p.order_id,
    p.status,
    p.captured,
    p.error_reason,
    p.observed_at
  from payment_attempts p
  join orders o on p.order_id = o.id
 where o.merchant_id = %(merchant_id)s
   and p.id <> %(payment_attempt_id)s
   and p.observed_at <= %(as_of)s
   and p.observed_at >= %(as_of)s - make_interval(days => %(lookback_days)s)
   and lower(trim(coalesce(p.raw_reference ->> %(identity_kind)s, ''))) = %(identity_value)s
 order by p.observed_at desc
"""


def summarize_customer_history(
    conn: psycopg.Connection,
    merchant_id: str,
    payment_attempt_id: str,
    raw_reference: dict[str, Any] | None,
    as_of: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> CustomerHistory | None:
    """Counts one payer's prior payment outcomes for one merchant.

    Returns None when the payment carries no usable identity -- callers
    must treat that as "no history available", never as "no history".
    """
    identity = extract_customer_identity(raw_reference)
    if identity is None:
        return None

    identity_value = str(raw_reference.get(identity.kind, "")).strip().lower()

    if as_of is None:
        with conn.cursor() as cur:
            cur.execute("select observed_at from payment_attempts where id = %s", (payment_attempt_id,))
            row = cur.fetchone()
            if row is None:
                return None
            as_of = row[0]

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _HISTORY_SQL,
            {
                "merchant_id": merchant_id,
                "payment_attempt_id": payment_attempt_id,
                "as_of": as_of,
                "lookback_days": lookback_days,
                "identity_kind": identity.kind,
                "identity_value": identity_value,
            },
        )
        rows = cur.fetchall()

    failure_reasons: dict[str, int] = {}
    for row in rows:
        if row["status"] == "failed":
            reason = row.get("error_reason") or "unspecified"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    times = [r["observed_at"] for r in rows if r["observed_at"] is not None]

    return CustomerHistory(
        identity_kind=identity.kind,
        identity_fingerprint=identity.fingerprint,
        lookback_days=lookback_days,
        prior_payment_count=len(rows),
        prior_captured_count=sum(1 for r in rows if r["status"] == "captured"),
        prior_authorized_count=sum(1 for r in rows if r["status"] == "authorized" and not r["captured"]),
        prior_failed_count=sum(1 for r in rows if r["status"] == "failed"),
        distinct_prior_orders=len({r["order_id"] for r in rows}),
        first_seen_at=min(times).isoformat() if times else None,
        last_seen_at=max(times).isoformat() if times else None,
        prior_failure_reasons=failure_reasons,
    )
