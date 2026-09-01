from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

# No update/delete exposed: a decisions row is immutable once written
# (Phase 2 Rev 2). A changed mind produces a new row referencing the same
# order_id/payment_attempt_id, never an edit of this one.


def insert_decision(
    conn: psycopg.Connection,
    merchant_id: str,
    order_id: str,
    payment_attempt_id: str | None,
    event_id: str,
    context_snapshot: dict[str, Any],
    expectation: dict[str, Any],
    decision_type: str,
    confidence: float,
    reason_codes: list[str],
    expected_impact: dict[str, Any],
    model_version: str,
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into decisions
                (merchant_id, order_id, payment_attempt_id, event_id, context_snapshot,
                 expectation, decision_type, confidence, reason_codes, expected_impact, model_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                merchant_id, order_id, payment_attempt_id, event_id,
                psycopg.types.json.Json(context_snapshot),
                psycopg.types.json.Json(expectation),
                decision_type, confidence,
                psycopg.types.json.Json(reason_codes),
                psycopg.types.json.Json(expected_impact),
                model_version,
            ),
        )
        return cur.fetchone()[0]


def get_decision(conn: psycopg.Connection, decision_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from decisions where id = %s", (decision_id,))
        return cur.fetchone()


def list_decisions_for_order(conn: psycopg.Connection, order_id: str) -> list[dict[str, Any]]:
    """Every Decision ever made for this order, oldest first. More than
    one row is expected and normal -- Decisions are immutable and a
    changed mind produces a new row, never an edit (see insert_decision's
    own comment)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from decisions where order_id = %s order by created_at asc", (order_id,))
        return cur.fetchall()
