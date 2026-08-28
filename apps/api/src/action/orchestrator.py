"""Action lifecycle orchestration + the Razorpay capture write boundary.

Narrowed per gate review to CAPTURE_PAYMENT execution only.
CUSTOMER_RETRY_PROMPT actions stop at AUTHORIZED in this gate -- there is
deliberately no dispatch to execution for them. That action type has no
external side effect this module performs and no meaningful Gate 8
verification target; prompt-action semantics are deferred to whatever
module owns the customer-facing workflow, not implemented here as a
silent no-op EXECUTED transition.

Frozen invariant: after one capture attempt, this module NEVER
automatically retries. A success response, a definite non-2xx error
response, and an ambiguous transport failure (timeout/connection error)
all route to the VERIFYING status alike -- none of them is self-declared
as a final verified-success or verified-failure outcome. Only Gate 9
(Verification) may set those two terminal statuses; nothing in this file
spells out their literal enum values (see
tests/test_architecture_boundaries.py, which checks that mechanically).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import psycopg

from domain.contracts import ActionType, compute_idempotency_key
from policy.orchestration import evaluate_decision
from razorpay_client.errors import RazorpayAPIError
from repository.actions import (
    DuplicateAction,
    claim_action_for_execution,
    get_action,
    get_action_by_idempotency_key,
    insert_action,
    update_action_status,
)
from repository.audit import insert_audit_entry
from repository.decisions import get_decision
from repository.payment_attempts import get_payment_attempt

from .razorpay_write_client import RazorpayWriteClient

# decision_type -> action_type. Both entries already existed conceptually
# from Gate 7; CUSTOMER_RETRY_PROMPT is included so propose_action() can
# still create and correctly audit those actions -- it just never
# dispatches them to execution (see _authorize below).
_DECISION_TYPE_TO_ACTION_TYPE: dict[str, str] = {
    "RECOMMEND_RETRY_PROMPT": "CUSTOMER_RETRY_PROMPT",
    "RECOMMEND_CAPTURE": "CAPTURE_PAYMENT",
}


def propose_action(
    conn: psycopg.Connection, decision_id: UUID | str, write_client: RazorpayWriteClient | None = None
) -> dict[str, Any]:
    decision = get_decision(conn, decision_id)
    if decision is None:
        raise ValueError(f"no decision found with id {decision_id}")
    if decision["payment_attempt_id"] is None:
        raise ValueError(f"decision {decision_id} has no payment_attempt_id -- cannot propose an action for it")

    # Writes the POLICY_EVALUATED audit entry itself (Gate 7 behavior,
    # reused as-is). Raises NotPolicyGated for NO_ACTION decisions --
    # allowed to propagate; proposing an action for a NO_ACTION decision
    # is a caller error.
    policy_evaluation = evaluate_decision(conn, decision_id)

    action_type = _DECISION_TYPE_TO_ACTION_TYPE[decision["decision_type"]]
    idempotency_key = compute_idempotency_key(
        str(decision["merchant_id"]), decision["order_id"], decision["payment_attempt_id"], ActionType(action_type)
    )

    try:
        action_id = insert_action(
            conn, str(decision_id), idempotency_key, action_type, policy_evaluation.model_dump(mode="json"), "PROPOSED"
        )
    except DuplicateAction:
        # Same real-world operation already has an action (possibly from
        # a different Decision) -- return the existing one, do not
        # attempt to propose or execute a second time.
        return get_action_by_idempotency_key(conn, idempotency_key)

    update_action_status(conn, action_id, "POLICY_EVALUATED")

    if not policy_evaluation.allowed:
        update_action_status(conn, action_id, "BLOCKED")
        insert_audit_entry(
            conn, "ACTION_BLOCKED", {"reason_codes": policy_evaluation.reason_codes},
            decision_id=str(decision_id), action_id=str(action_id),
        )
        return get_action(conn, action_id)

    if policy_evaluation.requires_approval:
        update_action_status(conn, action_id, "APPROVAL_PENDING")
        insert_audit_entry(conn, "APPROVAL_PENDING", {}, decision_id=str(decision_id), action_id=str(action_id))
        return get_action(conn, action_id)

    _authorize(conn, action_id, decision_id, write_client=write_client)
    return get_action(conn, action_id)


def grant_approval(
    conn: psycopg.Connection, action_id: UUID | str, approved_by: str, write_client: RazorpayWriteClient | None = None
) -> dict[str, Any]:
    action = get_action(conn, action_id)
    if action is None:
        raise ValueError(f"no action found with id {action_id}")
    if action["status"] != "APPROVAL_PENDING":
        raise ValueError(f"action {action_id} is not APPROVAL_PENDING (status={action['status']})")

    insert_audit_entry(
        conn, "APPROVAL_GRANTED", {"approved_by": approved_by},
        decision_id=str(action["decision_id"]), action_id=str(action_id),
    )
    _authorize(conn, action_id, action["decision_id"], write_client=write_client)
    return get_action(conn, action_id)


def reject_approval(conn: psycopg.Connection, action_id: UUID | str, reason: str) -> dict[str, Any]:
    action = get_action(conn, action_id)
    if action is None:
        raise ValueError(f"no action found with id {action_id}")
    if action["status"] != "APPROVAL_PENDING":
        raise ValueError(f"action {action_id} is not APPROVAL_PENDING (status={action['status']})")

    update_action_status(conn, action_id, "BLOCKED")
    insert_audit_entry(
        conn, "ACTION_BLOCKED", {"reason_codes": ["APPROVAL_REJECTED", reason]},
        decision_id=str(action["decision_id"]), action_id=str(action_id),
    )
    return get_action(conn, action_id)


def _authorize(
    conn: psycopg.Connection, action_id: UUID | str, decision_id: UUID | str,
    write_client: RazorpayWriteClient | None = None,
) -> None:
    update_action_status(conn, action_id, "AUTHORIZED")
    insert_audit_entry(conn, "ACTION_AUTHORIZED", {}, decision_id=str(decision_id), action_id=str(action_id))

    action = get_action(conn, action_id)
    if action["action_type"] == "CAPTURE_PAYMENT":
        execute_capture(conn, action_id, write_client=write_client)
    # CUSTOMER_RETRY_PROMPT: deliberately stops here. No EXECUTING, no
    # ACTION_EXECUTED, no dispatch at all -- see module docstring.


def execute_capture(
    conn: psycopg.Connection, action_id: UUID | str, write_client: RazorpayWriteClient | None = None
) -> dict[str, Any]:
    action = get_action(conn, action_id)
    if action is None:
        raise ValueError(f"no action found with id {action_id}")

    # Defense-in-depth re-check: reads the PERSISTED policy_evaluation,
    # not a value threaded through by the caller. Independent of, and in
    # addition to, the DB trigger guard_action_authorization
    # (0003_action_authorization_guard.sql).
    if not action["policy_evaluation"].get("allowed"):
        raise PermissionError(f"action {action_id} is not policy-authorized; refusing to execute")

    if not claim_action_for_execution(conn, action_id):
        # Another caller already claimed this action (or it wasn't
        # AUTHORIZED at all) -- back off, make zero Razorpay calls.
        return get_action(conn, action_id)

    decision = get_decision(conn, action["decision_id"])
    payment_attempt_id = decision["payment_attempt_id"]
    payment = get_payment_attempt(conn, payment_attempt_id)

    if payment is None or payment["status"] != "authorized":
        execution_reference: dict[str, Any] = {
            "outcome": "state_changed_before_execution",
            "known_status": payment["status"] if payment else None,
        }
    else:
        owns_client = write_client is None
        client = write_client or RazorpayWriteClient()
        try:
            execution_reference = _attempt_capture(client, payment_attempt_id, payment["amount"])
        finally:
            if owns_client:
                client.close()

    update_action_status(conn, action_id, "VERIFYING", execution_reference=execution_reference)
    insert_audit_entry(
        conn, "ACTION_EXECUTED", execution_reference, decision_id=str(action["decision_id"]), action_id=str(action_id)
    )
    return get_action(conn, action_id)


def _attempt_capture(client: RazorpayWriteClient, payment_id: str, amount: int) -> dict[str, Any]:
    """Exactly one attempt, ever, per call. The caller (execute_capture)
    never calls this twice for the same action -- no loop, no retry, no
    backoff. Success, a definite error response, and an ambiguous
    transport failure all produce a dict destined for VERYFING alike."""
    try:
        result = client.capture_payment(payment_id, amount)
    except RazorpayAPIError as exc:
        return {"outcome": "error_response", "http_status": exc.status_code, "detail": str(exc)}
    except httpx.HTTPError as exc:
        return {"outcome": "ambiguous_failure", "error_type": type(exc).__name__}
    else:
        return {"outcome": "success_response", "http_status": 200, "razorpay_status": result.get("status")}
