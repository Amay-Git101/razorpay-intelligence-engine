"""Action orchestration DB-integration tests. Requires live Postgres.

RECOMMEND_CAPTURE decisions are hand-constructed (same pattern as Gate 7
-- RuleBasedEngine still doesn't produce this decision_type). A
SpyWriteClient test double is injected in place of the real
RazorpayWriteClient so these tests make zero real network calls and need
no Razorpay credentials, while still exercising the exact same
orchestrator code path a real capture would use.
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

import httpx
import psycopg
import pytest

from action.orchestrator import (
    execute_capture,
    grant_approval,
    propose_action,
    reject_approval,
)
from domain.contracts import compute_idempotency_key, ActionType
from intelligence.orchestration import make_decision
from reconciliation.service import reconcile_order
from razorpay_client.errors import RazorpayAPIError
from repository.actions import (
    ActionNotPolicyAuthorized,
    claim_action_for_execution,
    get_action,
    insert_action,
)
from repository.audit import list_audit_trail_for_decision
from repository.canonical_events import list_events_for_order
from repository.decisions import insert_decision
from repository.merchants import insert_merchant


class SpyWriteClient:
    """Test double for RazorpayWriteClient. Records every call; the
    configured responder decides the outcome (return a dict for success,
    raise RazorpayAPIError/httpx.HTTPError to simulate a failure)."""

    def __init__(self, responder: Callable[[str, int], dict[str, Any]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or (lambda payment_id, amount: {"id": payment_id, "status": "captured", "captured": True})

    def capture_payment(self, payment_id: str, amount: int, currency: str = "INR") -> dict[str, Any]:
        self.calls.append({"payment_id": payment_id, "amount": amount, "currency": currency})
        return self._responder(payment_id, amount)


def _order_fixture(order_id: str, status: str, amount_paid: int, amount_due: int, attempts: int) -> dict[str, Any]:
    return {
        "id": order_id, "amount": 50000, "amount_paid": amount_paid, "amount_due": amount_due,
        "currency": "INR", "status": status, "attempts": attempts,
    }


def _payment_fixture(payment_id: str, order_id: str, status: str, captured: bool, **error_fields: Any) -> dict[str, Any]:
    return {
        "id": payment_id, "order_id": order_id, "status": status, "amount": 50000,
        "method": "card", "captured": captured,
        "error_source": error_fields.get("error_source"),
        "error_step": error_fields.get("error_step"),
        "error_reason": error_fields.get("error_reason"),
    }


class _FakeReadClient:
    def __init__(self, order: dict[str, Any], payments: list[dict[str, Any]]):
        self._order = order
        self._payments = payments

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _find_event(events: list[dict[str, Any]], event_type: str, entity_id: str | None = None) -> dict[str, Any]:
    for e in events:
        if e["event_type"] == event_type and (entity_id is None or e["entity_id"] == entity_id):
            return e
    raise AssertionError(f"no event {event_type} (entity_id={entity_id}) found")


def _reconcile_authorized_order(conn, merchant_id: str, order_id: str, payment_id: str, amount: int = 50000) -> None:
    client = _FakeReadClient(
        order=_order_fixture(order_id, "created", 0, amount, 1),
        payments=[_payment_fixture(payment_id, order_id, "authorized", False)],
    )
    reconcile_order(conn, client, merchant_id, order_id)


def _set_policy_config(conn: psycopg.Connection, merchant_id: str, config: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Json(config), merchant_id),
        )


def _insert_capture_decision(conn: psycopg.Connection, merchant_id: str, order_id: str, payment_attempt_id: str, amount: int) -> UUID:
    """Hand-constructed RECOMMEND_CAPTURE Decision -- RuleBasedEngine does
    not produce this decision_type (Gate 7 constraint, unchanged)."""
    return insert_decision(
        conn, merchant_id, order_id, payment_attempt_id,
        str(list_events_for_order(conn, order_id)[0]["id"]),
        {"fields": []}, {"bucket_key": "n/a", "expected_recovery_rate": 1.0, "sample_size": 0, "source": "test_fixture"},
        "RECOMMEND_CAPTURE", 1.0, ["TEST_FIXTURE_CAPTURE_RECOMMENDATION"],
        {"revenue_at_stake": amount}, "test_fixture",
    )


# ---------------------------------------------------------------------------
# Blocked capture makes zero Razorpay calls
# ---------------------------------------------------------------------------

def test_blocked_capture_makes_zero_razorpay_calls(db_conn, demo_merchant_id):
    order_id = "order_act_blocked"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=500000)
    spy = SpyWriteClient()

    action = propose_action(db_conn, decision_id, write_client=spy)

    assert action["status"] == "BLOCKED"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Approval-required capture makes zero calls before approval
# ---------------------------------------------------------------------------

def test_approval_required_capture_makes_zero_calls_before_approval(db_conn, demo_merchant_id):
    order_id = "order_act_pending"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    spy = SpyWriteClient()

    action = propose_action(db_conn, decision_id, write_client=spy)

    assert action["status"] == "APPROVAL_PENDING"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Approved capture makes exactly one write attempt
# ---------------------------------------------------------------------------

def test_approved_capture_makes_exactly_one_write_attempt(db_conn, demo_merchant_id):
    order_id = "order_act_approved"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    spy = SpyWriteClient()

    pending = propose_action(db_conn, decision_id, write_client=spy)
    assert pending["status"] == "APPROVAL_PENDING"
    assert spy.calls == []

    executed = grant_approval(db_conn, pending["id"], approved_by="test_merchant_owner", write_client=spy)

    assert len(spy.calls) == 1
    assert spy.calls[0] == {"payment_id": payment_id, "amount": 50000, "currency": "INR"}
    assert executed["status"] == "VERIFYING"
    assert executed["execution_reference"]["outcome"] == "success_response"


def test_auto_allowed_capture_executes_without_approval_step(db_conn, demo_merchant_id):
    order_id = "order_act_auto"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    spy = SpyWriteClient()

    action = propose_action(db_conn, decision_id, write_client=spy)

    assert len(spy.calls) == 1
    assert action["status"] == "VERIFYING"


# ---------------------------------------------------------------------------
# Duplicate action proposals cannot double-execute
# ---------------------------------------------------------------------------

def test_duplicate_action_proposal_cannot_double_execute(db_conn, demo_merchant_id):
    order_id = "order_act_dup"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id_1 = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    spy = SpyWriteClient()
    first = propose_action(db_conn, decision_id_1, write_client=spy)
    assert len(spy.calls) == 1

    # A second Decision recommending the SAME real-world operation
    # (same merchant/order/payment_attempt/action_type) must collide on
    # the same idempotency_key -- proposing an action for it must not
    # execute a second time.
    decision_id_2 = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    key1 = compute_idempotency_key(demo_merchant_id, order_id, payment_id, ActionType.CAPTURE_PAYMENT)
    key2 = compute_idempotency_key(demo_merchant_id, order_id, payment_id, ActionType.CAPTURE_PAYMENT)
    assert key1 == key2  # confirms the key is operation-scoped, not decision-scoped (Gate 2 rule)

    second = propose_action(db_conn, decision_id_2, write_client=spy)

    assert second["id"] == first["id"]  # returned the EXISTING action, not a new one
    assert len(spy.calls) == 1  # still just the one real attempt -- no double execution


def test_concurrent_claim_only_one_caller_wins(db_conn, demo_merchant_id):
    order_id = "order_act_cas"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 100000, "approval_band_upper": 500000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    idempotency_key = compute_idempotency_key(demo_merchant_id, order_id, payment_id, ActionType.CAPTURE_PAYMENT)
    action_id = insert_action(
        db_conn, str(decision_id), idempotency_key, "CAPTURE_PAYMENT",
        {"allowed": True, "policy_version": "policy_v1", "rules_evaluated": [], "authority_level_granted": "AUTOMATIC", "requires_approval": False, "reason_codes": []},
        "AUTHORIZED",
    )

    first_claim = claim_action_for_execution(db_conn, action_id)
    second_claim = claim_action_for_execution(db_conn, action_id)  # status is now EXECUTING, not AUTHORIZED

    assert first_claim is True
    assert second_claim is False


# ---------------------------------------------------------------------------
# Razorpay errors are handled without falsely marking execution successful
# ---------------------------------------------------------------------------

def test_razorpay_http_error_routes_to_verifying_not_a_verified_status(db_conn, demo_merchant_id):
    order_id = "order_act_httperror"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 100000, "approval_band_upper": 500000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)

    def failing_responder(pid, amt):
        raise RazorpayAPIError("Razorpay API returned HTTP 400 for capture", status_code=400)

    spy = SpyWriteClient(responder=failing_responder)
    action = propose_action(db_conn, decision_id, write_client=spy)

    assert len(spy.calls) == 1
    assert action["status"] == "VERIFYING"
    assert action["status"] not in ("VERIFIED_SUCCESS", "VERIFIED_FAILED")
    assert action["execution_reference"]["outcome"] == "error_response"
    assert action["execution_reference"]["http_status"] == 400


def test_ambiguous_transport_failure_routes_to_verifying_no_retry(db_conn, demo_merchant_id):
    order_id = "order_act_timeout"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 100000, "approval_band_upper": 500000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)

    def timeout_responder(pid, amt):
        raise httpx.ConnectTimeout("simulated ambiguous failure")

    spy = SpyWriteClient(responder=timeout_responder)
    action = propose_action(db_conn, decision_id, write_client=spy)

    assert len(spy.calls) == 1  # exactly one attempt -- no automatic retry
    assert action["status"] == "VERIFYING"
    assert action["execution_reference"]["outcome"] == "ambiguous_failure"


# ---------------------------------------------------------------------------
# Action cannot enter EXECUTING without policy authorization
# ---------------------------------------------------------------------------

def test_execute_capture_refuses_unauthorized_action_at_application_layer(db_conn, demo_merchant_id):
    order_id = "order_act_unauth"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    idempotency_key = compute_idempotency_key(demo_merchant_id, order_id, payment_id, ActionType.CAPTURE_PAYMENT)

    # Hand-inserted action stuck at POLICY_EVALUATED with allowed=False,
    # bypassing propose_action()'s normal flow entirely.
    action_id = insert_action(
        db_conn, str(decision_id), idempotency_key, "CAPTURE_PAYMENT",
        {"allowed": False, "policy_version": "policy_v1", "rules_evaluated": [], "authority_level_granted": "FORBIDDEN", "requires_approval": False, "reason_codes": ["TEST"]},
        "POLICY_EVALUATED",
    )
    spy = SpyWriteClient()

    with pytest.raises(PermissionError):
        execute_capture(db_conn, action_id, write_client=spy)
    assert spy.calls == []


def test_db_trigger_rejects_authorized_transition_with_disallowed_policy(db_conn, demo_merchant_id):
    order_id = "order_act_dbguard"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    idempotency_key = compute_idempotency_key(demo_merchant_id, order_id, payment_id, ActionType.CAPTURE_PAYMENT)

    action_id = insert_action(
        db_conn, str(decision_id), idempotency_key, "CAPTURE_PAYMENT",
        {"allowed": False, "policy_version": "policy_v1", "rules_evaluated": [], "authority_level_granted": "FORBIDDEN", "requires_approval": False, "reason_codes": ["TEST"]},
        "POLICY_EVALUATED",
    )

    from repository.actions import update_action_status
    with pytest.raises(ActionNotPolicyAuthorized):
        update_action_status(db_conn, action_id, "AUTHORIZED")


# ---------------------------------------------------------------------------
# Audit checkpoints recorded correctly
# ---------------------------------------------------------------------------

def test_audit_checkpoints_for_auto_allowed_flow(db_conn, demo_merchant_id):
    order_id = "order_act_audit_auto"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    spy = SpyWriteClient()
    propose_action(db_conn, decision_id, write_client=spy)

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(decision_id))]
    assert checkpoints == ["POLICY_EVALUATED", "ACTION_AUTHORIZED", "ACTION_EXECUTED"]


def test_audit_checkpoints_for_approval_flow(db_conn, demo_merchant_id):
    order_id = "order_act_audit_approval"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=spy)
    grant_approval(db_conn, action["id"], approved_by="owner", write_client=spy)

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(decision_id))]
    assert checkpoints == [
        "POLICY_EVALUATED", "APPROVAL_PENDING", "APPROVAL_GRANTED", "ACTION_AUTHORIZED", "ACTION_EXECUTED",
    ]


def test_audit_checkpoints_for_blocked_flow(db_conn, demo_merchant_id):
    order_id = "order_act_audit_blocked"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=500000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=500000)
    propose_action(db_conn, decision_id, write_client=SpyWriteClient())

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(decision_id))]
    assert checkpoints == ["POLICY_EVALUATED", "ACTION_BLOCKED"]


def test_rejected_approval_audits_correctly_and_never_executes(db_conn, demo_merchant_id):
    order_id = "order_act_rejected"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})

    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=50000)
    spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=spy)
    rejected = reject_approval(db_conn, action["id"], reason="merchant declined")

    assert rejected["status"] == "BLOCKED"
    assert spy.calls == []
    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(decision_id))]
    assert checkpoints == ["POLICY_EVALUATED", "APPROVAL_PENDING", "ACTION_BLOCKED"]


# ---------------------------------------------------------------------------
# CUSTOMER_RETRY_PROMPT stays out of scope for execution in this gate
# ---------------------------------------------------------------------------

def test_retry_prompt_action_stops_at_authorized_no_execution_attempted(db_conn, demo_merchant_id):
    order_id = "order_act_retryprompt"
    payment_id = f"pay_{order_id}"
    client = _FakeReadClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture(payment_id, order_id, "failed", False, error_source="gateway", error_step="payment_authorization", error_reason="payment_failed")],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = _find_event(list_events_for_order(db_conn, order_id), "payment.attempt.failed", payment_id)
    decision_id = make_decision(db_conn, demo_merchant_id, event)

    action = propose_action(db_conn, decision_id)  # no write_client needed -- must never be touched

    assert action["action_type"] == "CUSTOMER_RETRY_PROMPT"
    assert action["status"] == "AUTHORIZED"  # not EXECUTING, not EXECUTED, not VERIFYING

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(decision_id))]
    assert checkpoints == ["DECISION_CREATED", "POLICY_EVALUATED", "ACTION_AUTHORIZED"]
    assert "ACTION_EXECUTED" not in checkpoints


# ---------------------------------------------------------------------------
# Defensive guard against a payment_attempts row that changed underneath us
# ---------------------------------------------------------------------------

def test_state_changed_before_execution_makes_zero_razorpay_calls(db_conn, demo_merchant_id):
    order_id = "order_act_state_changed"
    payment_id = f"pay_{order_id}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 100000, "approval_band_upper": 500000})
    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)

    # Simulate the payment having already moved on (e.g. captured via a
    # separate path/reconciliation pass) before this action executes.
    with db_conn.cursor() as cur:
        cur.execute("update payment_attempts set status = 'captured', captured = true where id = %s", (payment_id,))

    spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=spy)

    assert spy.calls == []
    assert action["execution_reference"]["outcome"] == "state_changed_before_execution"


# ---------------------------------------------------------------------------
# Regression test: execution_reference (jsonb column, written via
# COALESCE in update_action_status) must persist correctly for every
# outcome path. This is a direct regression test for the
# psycopg.errors.CannotCoerce bug ("COALESCE could not convert type
# jsonb to json") caused by using the `Json` adapter (binds as PostgreSQL
# `json`) instead of `Jsonb` (binds as `jsonb`) when writing a jsonb
# column through COALESCE, where the json->jsonb assignment cast Postgres
# uses for plain `column = %s` writes does not apply. Re-fetches each
# action fresh from the database (not the in-memory return value) to
# prove the value actually round-trips through storage, not just that no
# exception was raised in-process.
# ---------------------------------------------------------------------------

def test_execution_reference_persists_for_every_outcome_path(db_conn, demo_merchant_id):
    lenient_config = {"max_auto_capture_amount": 100000, "approval_band_upper": 500000}

    # 1. success_response
    order_a = "order_act_persist_success"
    payment_a = f"pay_{order_a}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_a, payment_a, amount=10000)
    _set_policy_config(db_conn, demo_merchant_id, lenient_config)
    decision_a = _insert_capture_decision(db_conn, demo_merchant_id, order_a, payment_a, amount=10000)
    action_a = propose_action(db_conn, decision_a, write_client=SpyWriteClient())
    reloaded_a = get_action(db_conn, action_a["id"])
    assert reloaded_a["status"] == "VERIFYING"
    assert reloaded_a["execution_reference"]["outcome"] == "success_response"
    assert reloaded_a["execution_reference"]["razorpay_status"] == "captured"

    # 2. error_response
    order_b = "order_act_persist_error"
    payment_b = f"pay_{order_b}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_b, payment_b, amount=10000)
    decision_b = _insert_capture_decision(db_conn, demo_merchant_id, order_b, payment_b, amount=10000)

    def failing_responder(pid, amt):
        raise RazorpayAPIError("Razorpay API returned HTTP 400 for capture", status_code=400)

    action_b = propose_action(db_conn, decision_b, write_client=SpyWriteClient(responder=failing_responder))
    reloaded_b = get_action(db_conn, action_b["id"])
    assert reloaded_b["status"] == "VERIFYING"
    assert reloaded_b["execution_reference"]["outcome"] == "error_response"
    assert reloaded_b["execution_reference"]["http_status"] == 400

    # 3. ambiguous_failure
    order_c = "order_act_persist_ambiguous"
    payment_c = f"pay_{order_c}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_c, payment_c, amount=10000)
    decision_c = _insert_capture_decision(db_conn, demo_merchant_id, order_c, payment_c, amount=10000)

    def timeout_responder(pid, amt):
        raise httpx.ConnectTimeout("simulated ambiguous failure")

    action_c = propose_action(db_conn, decision_c, write_client=SpyWriteClient(responder=timeout_responder))
    reloaded_c = get_action(db_conn, action_c["id"])
    assert reloaded_c["status"] == "VERIFYING"
    assert reloaded_c["execution_reference"]["outcome"] == "ambiguous_failure"
    assert reloaded_c["execution_reference"]["error_type"] == "ConnectTimeout"

    # 4. state_changed_before_execution
    order_d = "order_act_persist_statechanged"
    payment_d = f"pay_{order_d}"
    _reconcile_authorized_order(db_conn, demo_merchant_id, order_d, payment_d, amount=10000)
    decision_d = _insert_capture_decision(db_conn, demo_merchant_id, order_d, payment_d, amount=10000)
    with db_conn.cursor() as cur:
        cur.execute("update payment_attempts set status = 'captured', captured = true where id = %s", (payment_d,))
    action_d = propose_action(db_conn, decision_d, write_client=SpyWriteClient())
    reloaded_d = get_action(db_conn, action_d["id"])
    assert reloaded_d["execution_reference"]["outcome"] == "state_changed_before_execution"
    assert reloaded_d["execution_reference"]["known_status"] == "captured"
