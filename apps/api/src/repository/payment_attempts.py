from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


class InvalidPaymentAttemptTransition(Exception):
    """Raised when the database's transition guard trigger rejects a
    write. Callers (the future reconciliation module) are expected to
    catch this and record a payment.attempt.anomaly canonical event +
    RECONCILIATION_ANOMALY audit entry instead of retrying the write."""


def get_payment_attempt(conn: psycopg.Connection, payment_attempt_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from payment_attempts where id = %s", (payment_attempt_id,))
        return cur.fetchone()


def list_payment_attempts_for_order(conn: psycopg.Connection, order_id: str) -> list[dict[str, Any]]:
    """Authoritative source for 'how many attempts exist on this order' --
    the context builder derives attempt_number from this, never from
    canonical_events (which can contain non-payment-attempt-count
    entries, e.g. an anomaly event or an order.paid event)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from payment_attempts where order_id = %s order by observed_at asc",
            (order_id,),
        )
        return cur.fetchall()


def insert_payment_attempt(
    conn: psycopg.Connection,
    payment_attempt_id: str,
    order_id: str,
    status: str,
    method: str | None,
    captured: bool,
    error_source: str | None,
    error_step: str | None,
    error_reason: str | None,
    amount: int,
    raw_reference: dict[str, Any],
) -> None:
    """Initial insert for a payment id never seen before. Distinct new
    attempts always get a new row -- this function never updates an
    existing id (see update_payment_attempt_status for that path, which
    is guarded by the DB transition trigger)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into payment_attempts
                (id, order_id, status, method, captured, error_source,
                 error_step, error_reason, amount, raw_reference)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                payment_attempt_id, order_id, status, method, captured,
                error_source, error_step, error_reason, amount,
                psycopg.types.json.Json(raw_reference),
            ),
        )


def update_payment_attempt_status(
    conn: psycopg.Connection,
    payment_attempt_id: str,
    new_status: str,
    captured: bool,
    raw_reference: dict[str, Any],
) -> None:
    """Attempts an in-place status transition on an existing row. The
    database's guard_payment_attempt_transition trigger is the
    authoritative enforcement of the Phase 2 Rev 2 validity matrix -- an
    invalid (old, new) pair raises a Postgres exception (SQLSTATE P0001),
    which this function translates into InvalidPaymentAttemptTransition
    so callers never mistake it for an ordinary DB error."""
    try:
        # conn.transaction() opens a SAVEPOINT here (the connection is
        # already inside an outer transaction by this point in normal
        # use). On the trigger's raised exception, only this savepoint is
        # rolled back -- everything committed/staged earlier in the
        # surrounding transaction is preserved. Using conn.rollback()
        # here would instead abort the *entire* connection-level
        # transaction, silently discarding unrelated prior work.
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update payment_attempts
                    set status = %s, captured = %s, raw_reference = %s, observed_at = now()
                    where id = %s
                    """,
                    (new_status, captured, psycopg.types.json.Json(raw_reference), payment_attempt_id),
                )
    except psycopg.errors.RaiseException as exc:
        raise InvalidPaymentAttemptTransition(str(exc)) from exc
