from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


class DuplicateAction(Exception):
    """Raised when an insert collides with an existing idempotency_key.
    Callers should treat this as 'already handled', never as a reason to
    retry the underlying operation."""


class ActionNotPolicyAuthorized(Exception):
    """Raised when the database's guard_action_authorization trigger
    (0003_action_authorization_guard.sql) rejects a transition into
    AUTHORIZED/EXECUTING because the row's persisted
    policy_evaluation.allowed is not true. Defense-in-depth: this should
    never fire if application code (src/action/orchestrator.py) is
    correct, but if it does, it must not be mistaken for an ordinary DB
    error or silently swallowed."""


def insert_action(
    conn: psycopg.Connection,
    decision_id: str,
    idempotency_key: str,
    action_type: str,
    policy_evaluation: dict[str, Any],
    status: str,
) -> UUID:
    try:
        # Same savepoint reasoning as
        # payment_attempts.update_payment_attempt_status: a plain
        # conn.rollback() here would abort the entire surrounding
        # transaction, not just this insert. This was not exercised by
        # the currently-failing tests, but is the same bug class and is
        # fixed proactively for consistency.
        with conn.transaction():
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
        raise DuplicateAction(idempotency_key) from exc


def update_action_status(
    conn: psycopg.Connection,
    action_id: UUID,
    status: str,
    execution_reference: dict[str, Any] | None = None,
    verification_result: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> None:
    try:
        # Savepoint, not conn.rollback() on failure -- same reasoning as
        # payment_attempts.update_payment_attempt_status. A rejection
        # from guard_action_authorization must only undo this statement,
        # never the surrounding transaction.
        #
        # Jsonb (not Json) is required here specifically: COALESCE(%s,
        # jsonb_column) needs both operands typed as jsonb to resolve at
        # all. json -> jsonb is only an ASSIGNMENT cast in Postgres --
        # valid for plain `column = %s`, but COALESCE's type resolution
        # doesn't consult assignment casts, so a `Json`-wrapped parameter
        # (sent as `json`) fails there with "could not convert type
        # jsonb to json". Every other repository write in this project
        # uses plain assignment and is unaffected; this is the only
        # COALESCE-based write in the schema.
        with conn.transaction():
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
                        psycopg.types.json.Jsonb(execution_reference) if execution_reference is not None else None,
                        psycopg.types.json.Jsonb(verification_result) if verification_result is not None else None,
                        psycopg.types.json.Jsonb(outcome) if outcome is not None else None,
                        action_id,
                    ),
                )
    except psycopg.errors.RaiseException as exc:
        raise ActionNotPolicyAuthorized(str(exc)) from exc


def claim_action_for_execution(conn: psycopg.Connection, action_id: UUID | str) -> bool:
    """Compare-and-swap AUTHORIZED -> EXECUTING. Returns True only for
    the caller that actually wins the claim; a concurrent caller (or one
    that arrives after the action is no longer AUTHORIZED) gets False
    and must not proceed to call Razorpay."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "update actions set status = 'EXECUTING', updated_at = now() "
                    "where id = %s and status = 'AUTHORIZED'",
                    (action_id,),
                )
                return cur.rowcount == 1
    except psycopg.errors.RaiseException as exc:
        raise ActionNotPolicyAuthorized(str(exc)) from exc


def get_action_for_update(conn: psycopg.Connection, action_id: UUID | str) -> dict[str, Any] | None:
    """Row-level lock (SELECT ... FOR UPDATE), used by
    verification.verifier.verify_action() to serialize concurrent
    verification attempts on the same action. Must be called inside an
    open transaction (conn.transaction()) for the lock to hold beyond
    this single statement -- holding it for the whole verify_action()
    call ensures two simultaneous callers cannot both read a stale
    verification_result, both increment the read-attempt counter past
    the bound, or both write a terminal VERIFICATION_COMPLETED audit
    entry."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from actions where id = %s for update", (action_id,))
        return cur.fetchone()


def get_action(conn: psycopg.Connection, action_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from actions where id = %s", (action_id,))
        return cur.fetchone()


def get_action_by_idempotency_key(conn: psycopg.Connection, idempotency_key: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from actions where idempotency_key = %s", (idempotency_key,))
        return cur.fetchone()
