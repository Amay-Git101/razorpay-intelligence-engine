from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


class DuplicateAction(Exception):
    """Raised when an insert collides with an existing idempotency_key.
    Callers should treat this as 'already handled', never as a reason to
    retry the underlying operation."""


def insert_action(
    conn: psycopg.Connection,
    decision_id: str,
    idempotency_key: str,
    action_type: str,
    policy_evaluation: dict[str, Any],
    status: str,
) -> UUID:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into actions (decision_id, idempotency_key, action_type, policy_evaluation, status)
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                (decision_id, idempotency_key, action_type, psycopg.types.json.Json(policy_evaluation), status),
            )
            return cur.fetchone()[0]
    except psycopg.errors.UniqueViolation as exc:
        conn.rollback()
        raise DuplicateAction(idempotency_key) from exc


def update_action_status(
    conn: psycopg.Connection,
    action_id: UUID,
    status: str,
    execution_reference: dict[str, Any] | None = None,
    verification_result: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update actions
            set status = %s,
                execution_reference = coalesce(%s, execution_reference),
                verification_result = coalesce(%s, verification_result),
                outcome = coalesce(%s, outcome),
                updated_at = now()
            where id = %s
            """,
            (
                status,
                psycopg.types.json.Json(execution_reference) if execution_reference is not None else None,
                psycopg.types.json.Json(verification_result) if verification_result is not None else None,
                psycopg.types.json.Json(outcome) if outcome is not None else None,
                action_id,
            ),
        )


def get_action(conn: psycopg.Connection, action_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from actions where id = %s", (action_id,))
        return cur.fetchone()


def get_action_by_idempotency_key(conn: psycopg.Connection, idempotency_key: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from actions where idempotency_key = %s", (idempotency_key,))
        return cur.fetchone()
