from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


def insert_batch(
    conn: psycopg.Connection,
    merchant_id: str,
    source: str,
    detection_version: str,
) -> UUID:
    """Creates an empty batch. detected_count/revenue_at_risk are set by
    finalize_batch() once every item has been inserted, so a batch is never
    briefly visible with a total that does not match its own items."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into recovery_batches (merchant_id, source, detection_version)
            values (%s, %s, %s)
            returning id
            """,
            (merchant_id, source, detection_version),
        )
        return cur.fetchone()[0]


def insert_batch_item(
    conn: psycopg.Connection,
    batch_id: UUID | str,
    order_id: str,
    payment_attempt_id: str,
    amount_at_risk: int,
    risk_reason_codes: list[str],
) -> UUID | None:
    """Returns the new item id, or None if this payment attempt is already in
    this batch. The unique (batch_id, payment_attempt_id) constraint is what
    guarantees the same at-risk money can never be counted twice in one
    batch's denominator; on conflict we do nothing rather than raising,
    because re-running detection over an existing batch is a legitimate
    operation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into recovery_batch_items
                (batch_id, order_id, payment_attempt_id, amount_at_risk, risk_reason_codes)
            values (%s, %s, %s, %s, %s)
            on conflict (batch_id, payment_attempt_id) do nothing
            returning id
            """,
            (batch_id, order_id, payment_attempt_id, amount_at_risk, psycopg.types.json.Json(risk_reason_codes)),
        )
        row = cur.fetchone()
        return row[0] if row else None


def finalize_batch(conn: psycopg.Connection, batch_id: UUID | str) -> dict[str, Any]:
    """Recomputes the batch totals FROM the batch's own items.

    The totals are never accumulated in Python and written down separately:
    they are a projection of the rows that actually exist, so the header can
    never disagree with the detail. This is the number reported as "revenue
    at risk", so it has to be derivable, not asserted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update recovery_batches b
               set detected_count  = coalesce(i.item_count, 0),
                   revenue_at_risk = coalesce(i.total_at_risk, 0)
              from (
                  select count(*) as item_count, sum(amount_at_risk) as total_at_risk
                    from recovery_batch_items
                   where batch_id = %s
              ) i
             where b.id = %s
            """,
            (batch_id, batch_id),
        )
    return get_batch(conn, batch_id)


def link_item_decision(
    conn: psycopg.Connection, batch_id: UUID | str, payment_attempt_id: str, decision_id: UUID | str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update recovery_batch_items
               set decision_id = %s
             where batch_id = %s and payment_attempt_id = %s
            """,
            (decision_id, batch_id, payment_attempt_id),
        )


def get_batch(conn: psycopg.Connection, batch_id: UUID | str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from recovery_batches where id = %s", (batch_id,))
        return cur.fetchone()


def list_batches_for_merchant(
    conn: psycopg.Connection, merchant_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from recovery_batches where merchant_id = %s order by created_at desc limit %s",
            (merchant_id, limit),
        )
        return cur.fetchall()


def list_batch_items(conn: psycopg.Connection, batch_id: UUID | str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from recovery_batch_items where batch_id = %s order by amount_at_risk desc",
            (batch_id,),
        )
        return cur.fetchall()


def list_unprocessed_items(conn: psycopg.Connection, batch_id: UUID | str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select * from recovery_batch_items
             where batch_id = %s and decision_id is null
             order by amount_at_risk desc
            """,
            (batch_id,),
        )
        return cur.fetchall()


# The batch detail view: each at-risk payment alongside whatever the pipeline
# decided and did about it. LEFT JOINs throughout, so an item that has not
# been processed yet appears with nulls rather than disappearing from the
# list -- the "not yet processed" state is real and must stay visible.
_ITEMS_WITH_OUTCOMES_SQL = """
select
    i.payment_attempt_id,
    i.order_id,
    i.amount_at_risk,
    i.risk_reason_codes,
    p.error_reason,
    p.error_source,
    p.method,
    d.id            as decision_id,
    d.decision_type,
    d.confidence,
    d.reason_codes,
    d.model_version,
    d.context_snapshot,
    a.id            as action_id,
    a.action_type,
    a.status        as action_status,
    a.outcome
from recovery_batch_items i
join payment_attempts p on i.payment_attempt_id = p.id
left join decisions d on i.decision_id = d.id
left join actions a on a.decision_id = d.id
where i.batch_id = %s
order by i.amount_at_risk desc
"""


def list_batch_items_with_outcomes(conn: psycopg.Connection, batch_id: UUID | str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ITEMS_WITH_OUTCOMES_SQL, (batch_id,))
        return cur.fetchall()


def list_recent_batches(conn: psycopg.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Most recent batches across all merchants, with the merchant's name.

    Exists so a client can find the batches worth showing without knowing any
    merchant id in advance -- the frontend must not hardcode a demo identifier,
    and iterating every merchant to ask whether it has a batch does not scale.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select b.*, m.name as merchant_name
            from recovery_batches b
            join merchants m on b.merchant_id = m.id
            order by b.created_at desc, b.id desc
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()
