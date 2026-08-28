"""Verification: the ONLY module permitted to declare VERIFIED_SUCCESS,
VERIFIED_FAILED, or ESCALATED. Consumes actions Gate 8 left at VERYFING.

Core principle: every verdict comes exclusively from a fresh
GET /v1/payments/:id response (the VERIFIED read capability from Gate
3) -- never from execution_reference, which is only Action's own
unverified account of what it attempted. Even a "success_response"
execution_reference is re-confirmed against live state, not trusted.

Read-only: only ever calls RazorpayReadClient.get_payment(). Never
imports or constructs a RazorpayWriteClient -- enforced mechanically by
tests/test_architecture_boundaries.py, not just this docstring.

Locked decisions from gate review:
  - Up to MAX_READ_ATTEMPTS (3) actual Razorpay reads across separate
    verify_action() calls, tracked in actions.verification_result
    (no new column).
  - VERIFICATION_COMPLETED is reused for VERIFIED_SUCCESS,
    VERIFIED_FAILED, and ESCALATED alike (no new audit checkpoint).
  - Escalation is automatic -- no human acknowledgement step. It fires
    once the read-attempt bound is exhausted, or immediately on an
    unexpected-but-successfully-read payment status (retrying a read
    only makes sense for a READ FAILURE; an unexpected status won't
    change by reading again).
  - Concurrency: the action row is locked (SELECT ... FOR UPDATE) for
    the whole call, serializing simultaneous verify_action() calls on
    the same action_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from razorpay_client.client import RazorpayReadClient, RazorpayReadClientProtocol
from razorpay_client.errors import RazorpayAPIError
from repository.actions import get_action, get_action_for_update, update_action_status
from repository.audit import insert_audit_entry
from repository.decisions import get_decision
from repository.payment_attempts import get_payment_attempt

MAX_READ_ATTEMPTS = 3

_TERMINAL_STATUSES = {"VERIFIED_SUCCESS", "VERIFIED_FAILED", "ESCALATED"}
_VERIFIABLE_STATUSES = {"VERIFYING", "VERIFICATION_UNCERTAIN"}


def verify_action(
    conn: psycopg.Connection, action_id: UUID | str, read_client: RazorpayReadClientProtocol | None = None
) -> dict[str, Any]:
    with conn.transaction():
        action = get_action_for_update(conn, action_id)
        if action is None:
            raise ValueError(f"no action found with id {action_id}")

        if action["status"] in _TERMINAL_STATUSES:
            return action  # idempotent no-op -- already resolved, zero additional reads

        if action["status"] not in _VERIFIABLE_STATUSES:
            raise ValueError(f"action {action_id} is not in a verifiable state (status={action['status']})")

        verification_result: dict[str, Any] = action["verification_result"] or {"read_attempts": [], "attempt_count": 0}
        attempt_count = verification_result.get("attempt_count", 0)

        if attempt_count >= MAX_READ_ATTEMPTS:
            # Safety net: should already have been escalated by whichever
            # call hit the bound, but never leave an action silently
            # stuck if it somehow wasn't.
            return _finalize(conn, action, "ESCALATED", verification_result, "READ_ATTEMPT_BOUND_ALREADY_EXHAUSTED")

        decision = get_decision(conn, action["decision_id"])
        payment_attempt_id = decision["payment_attempt_id"]
        payment_row = get_payment_attempt(conn, payment_attempt_id)
        expected_amount = payment_row["amount"] if payment_row else None

        owns_client = read_client is None
        client = read_client or RazorpayReadClient()
        fetched: dict[str, Any] | None = None
        read_error: str | None = None
        try:
            fetched = client.get_payment(payment_attempt_id)
        except RazorpayAPIError as exc:
            read_error = str(exc)
        finally:
            if owns_client:
                client.close()

        attempt_count += 1
        verification_result["attempt_count"] = attempt_count
        verification_result.setdefault("read_attempts", []).append(
            {
                "attempt": attempt_count,
                "success": fetched is not None,
                "error": read_error,
                "fetched_status": fetched.get("status") if fetched else None,
                "fetched_amount": fetched.get("amount") if fetched else None,
            }
        )

        if fetched is None:
            if attempt_count >= MAX_READ_ATTEMPTS:
                return _finalize(
                    conn, action, "ESCALATED", verification_result, "VERIFICATION_READ_FAILED_BOUND_EXHAUSTED"
                )
            update_action_status(conn, action["id"], "VERIFICATION_UNCERTAIN", verification_result=verification_result)
            return get_action(conn, action["id"])

        if fetched.get("status") == "captured" and fetched.get("amount") == expected_amount:
            return _finalize(conn, action, "VERIFIED_SUCCESS", verification_result, "CAPTURED_CONFIRMED")
        if fetched.get("status") == "authorized":
            return _finalize(conn, action, "VERIFIED_FAILED", verification_result, "STILL_AUTHORIZED_NOT_CAPTURED")

        # Any other observed status is unexpected for this project's
        # scope -- escalate immediately. Re-reading only makes sense for
        # a definite read FAILURE (handled above); a successfully-read
        # but unrecognized status won't change by reading again.
        return _finalize(
            conn, action, "ESCALATED", verification_result, f"UNEXPECTED_PAYMENT_STATUS:{fetched.get('status')}"
        )


def _finalize(
    conn: psycopg.Connection,
    action: dict[str, Any],
    terminal_status: str,
    verification_result: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    verification_result["result"] = terminal_status
    verification_result["reason"] = reason

    outcome = None
    if terminal_status == "VERIFIED_SUCCESS":
        last_attempt = verification_result["read_attempts"][-1]
        created_at: datetime = action["created_at"]
        now = datetime.now(timezone.utc)
        outcome = {
            "recovered_amount": last_attempt["fetched_amount"],
            "verified_at": now.isoformat(),
            "time_to_resolution_seconds": (now - created_at).total_seconds(),
        }

    update_action_status(conn, action["id"], terminal_status, verification_result=verification_result, outcome=outcome)
    insert_audit_entry(
        conn, "VERIFICATION_COMPLETED", {"result": terminal_status, "reason": reason},
        decision_id=str(action["decision_id"]), action_id=str(action["id"]),
    )
    return get_action(conn, action["id"])
