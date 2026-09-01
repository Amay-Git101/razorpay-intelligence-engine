from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


def upsert_order(
    conn: psycopg.Connection,
    order_id: str,
    merchant_id: str,
    amount: int,
    amount_paid: int,
    amount_due: int,
    status: str,
    attempts: int,
    currency: str,
    raw_reference: dict[str, Any],
) -> None:
    """Order aggregate state is refreshed wholesale from an authoritative
    fetch -- unlike payment_attempts, there is no documented intermediate
    transition risk here worth guarding at the DB layer (order.status
    only ever moves forward per Phase 1 evidence)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into orders (id, merchant_id, amount, amount_paid, amount_due,
                                 status, attempts, currency, raw_reference)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (id) do update set
                amount_paid = excluded.amount_paid,
                amount_due = excluded.amount_due,
                status = excluded.status,
                attempts = excluded.attempts,
                raw_reference = excluded.raw_reference,
                observed_at = now()
            """,
            (
                order_id, merchant_id, amount, amount_paid, amount_due,
                status, attempts, currency, psycopg.types.json.Json(raw_reference),
            ),
        )


def get_order(conn: psycopg.Connection, order_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from orders where id = %s", (order_id,))
        return cur.fetchone()


def list_orders_for_merchant(conn: psycopg.Connection, merchant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from orders where merchant_id = %s order by observed_at desc limit %s",
            (merchant_id, limit),
        )
        return cur.fetchall()
