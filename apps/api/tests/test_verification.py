"""Verification DB-integration tests. Requires live Postgres.

Every test's core assertion is that the VERDICT comes from the live
GET /v1/payments/:id fetch, never from Action's own execution_reference
-- several tests deliberately set up an execution_reference that
contradicts the live fetch, to prove independence rather than assuming
it.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Callable

import psycopg
import pytest

from action.orchestrator import propose_action
from razorpay_client.errors import RazorpayAPIError
from reconciliation.service import reconcile_order
from repository.actions import get_action, get_action_for_update
from repository.audit import list_audit_trail_for_decision
from repository.canonical_events import list_events_for_order
from repository.decisions import insert_decision
from verification.verifier import MAX_READ_ATTEMPTS, verify_action


class SpyWriteClient:
    """Same test double used in Gate 8's tests -- reused here only to
    legitimately produce a real VERIFYING action via the actual
    propose_action() flow, rather than hand-crafting rows."""

    def __init__(self, responder: Callable[[str, int], dict[str, Any]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or (lambda pid, amt: {"id": pid, "status": "captured", "captured": True})

    def capture_payment(self, payment_id: str, amount: int, currency: str = "INR") -> dict[str, Any]:
        self.calls.append({"payment_id": payment_id, "amount": amount, "currency": currency})
        return self._responder(payment_id, amount)


class SpyReadClient:
    """Test double for RazorpayReadClientProtocol -- only get_payment is
    used by verify_action()."""

    def __init__(self, responder: Callable[[str], dict[str, Any]]):
        self.calls: list[str] = []
        self._responder = responder

    def get_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        self.calls.append(payment_id)
        return self._responder(payment_id)


def _sequence_responder(*outcomes: dict[str, Any] | Exception) -> Callable[[str], dict[str, Any]]:
    it = iter(outcomes)

    def responder(payment_id: str) -> dict[str, Any]:
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return responder


def _order_fixture(order_id: str, status: str, amount_paid: int, amount_due: int, attempts: int) -> dict[str, Any]:
    return {
        "id": order_id, "amount": 50000, "amount_paid": amount_paid, "amount_due": amount_due,
        "currency": "INR", "status": status, "attempts": attempts,
    }


def _payment_fixture(
    payment_id: str, order_id: str, status: str, captured: bool, amount: int = 50000, **error_fields: Any
) -> dict[str, Any]:
    return {
        "id": payment_id, "order_id": order_id, "status": status, "amount": amount,
        "method": "card", "captured": captured,
        "error_source": error_fields.get("error_source"),
        "error_step": error_fields.get("error_step"),
        "error_reason": error_fields.get("error_reason"),
    }


class _FakeReadClientForReconciliation:
    def __init__(self, order: dict[str, Any], payments: list[dict[str, Any]]):
        self._order = order
        self._payments = payments

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _set_policy_config(conn: psycopg.Connection, merchant_id: str, config: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Jsonb(config), merchant_id),
        )


def _insert_capture_decision(conn: psycopg.Connection, merchant_id: str, order_id: str, payment_attempt_id: str, amount: int):
    return insert_decision(
        conn, merchant_id, order_id, payment_attempt_id,
        str(list_events_for_order(conn, order_id)[0]["id"]),
        {"fields": []}, {"bucket_key": "n/a", "expected_recovery_rate": 1.0, "sample_size": 0, "source": "test_fixture"},
        "RECOMMEND_CAPTURE", 1.0, ["TEST_FIXTURE_CAPTURE_RECOMMENDATION"],
        {"revenue_at_stake": amount}, "test_fixture",
    )


def _make_verifying_action(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000,
    write_responder: Callable[[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produces a real action at VERIFYING via the actual Gate 8
    propose_action() flow (auto-allowed capture), not a hand-crafted row."""
    payment_id = f"pay_{order_id}"
    client = _FakeReadClientForReconciliation(
        order=_order_fixture(order_id, "created", 0, amount, 1),
        payments=[_payment_fixture(payment_id, order_id, "authorized", False, amount=amount)],
    )
    reconcile_order(conn, client, merchant_id, order_id)
    _set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    decision_id = _insert_capture_decision(conn, merchant_id, order_id, payment_id, amount=amount)
    spy = SpyWriteClient(responder=write_responder)
    action = propose_action(conn, decision_id, write_client=spy)
    assert action["status"] == "VERIFYING"
    return action


# ---------------------------------------------------------------------------
# Verdict comes exclusively from the live fetch
# ---------------------------------------------------------------------------

def test_success_response_confirmed_by_live_fetch_is_verified_success(db_conn, demo_merchant_id):
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_success", amount=10000)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})

    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_SUCCESS"
    assert len(read_client.calls) == 1
    assert result["outcome"]["recovered_amount"] == 10000
    assert "verified_at" in result["outcome"]
    assert "time_to_resolution_seconds" in result["outcome"]


def test_error_response_but_live_fetch_shows_captured_is_still_verified_success(db_conn, demo_merchant_id):
    def failing_write(pid, amt):
        raise RazorpayAPIError("Razorpay API returned HTTP 400", status_code=400)

    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_stale_error", amount=10000, write_responder=failing_write)
    assert action["execution_reference"]["outcome"] == "error_response"

    # Live fetch says it actually IS captured -- the earlier error was
    # stale/misleading. Verdict must follow the live fetch, not the
    # stored execution_reference.
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})
    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_SUCCESS"


def test_error_response_and_live_fetch_confirms_still_authorized_is_verified_failed(db_conn, demo_merchant_id):
    def failing_write(pid, amt):
        raise RazorpayAPIError("Razorpay API returned HTTP 400", status_code=400)

    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_confirmed_fail", amount=10000, write_responder=failing_write)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "authorized", "amount": 10000})

    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_FAILED"
    assert result["outcome"] is None


def test_captured_status_with_mismatched_amount_escalates_rather_than_succeeds(db_conn, demo_merchant_id):
    # Regression test: a live fetch reporting status="captured" is NOT
    # sufficient on its own -- the amount must also match the authoritative
    # payment_attempts row. A captured status against a different amount
    # (e.g. a partial capture, or a mismatched payment_id somehow) must
    # never be treated as success; it's an unexpected situation this
    # project has no rule for, so it escalates rather than guessing.
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_amount_mismatch", amount=10000)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 9999})

    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "ESCALATED"
    assert result["verification_result"]["reason"] == "UNEXPECTED_PAYMENT_STATUS:captured"


def test_ambiguous_failure_live_fetch_shows_captured_is_verified_success(db_conn, demo_merchant_id):
    def timeout_write(pid, amt):
        import httpx
        raise httpx.ConnectTimeout("simulated ambiguous failure")

    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_ambig_success", amount=10000, write_responder=timeout_write)
    assert action["execution_reference"]["outcome"] == "ambiguous_failure"

    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})
    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_SUCCESS"


def test_ambiguous_failure_live_fetch_shows_authorized_is_verified_failed(db_conn, demo_merchant_id):
    def timeout_write(pid, amt):
        import httpx
        raise httpx.ConnectTimeout("simulated ambiguous failure")

    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_ambig_fail", amount=10000, write_responder=timeout_write)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "authorized", "amount": 10000})

    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_FAILED"


def test_state_changed_before_execution_live_fetch_shows_captured_is_verified_success(db_conn, demo_merchant_id):
    order_id = "order_ver_statechanged"
    payment_id = f"pay_{order_id}"
    client = _FakeReadClientForReconciliation(
        order=_order_fixture(order_id, "created", 0, 10000, 1),
        payments=[_payment_fixture(payment_id, order_id, "authorized", False, amount=10000)],
    )
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    _set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    decision_id = _insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)

    with db_conn.cursor() as cur:
        cur.execute("update payment_attempts set status = 'captured', captured = true where id = %s", (payment_id,))

    spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=spy)
    assert spy.calls == []
    assert action["execution_reference"]["outcome"] == "state_changed_before_execution"

    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})
    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "VERIFIED_SUCCESS"


# ---------------------------------------------------------------------------
# Bounded retry + automatic escalation
# ---------------------------------------------------------------------------

def test_read_failures_retry_then_succeed_within_bound(db_conn, demo_merchant_id):
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_retry_then_success", amount=10000)
    read_client = SpyReadClient(_sequence_responder(
        RazorpayAPIError("HTTP 503", status_code=503),
        {"id": "x", "status": "captured", "amount": 10000},
    ))

    first = verify_action(db_conn, action["id"], read_client=read_client)
    assert first["status"] == "VERIFICATION_UNCERTAIN"
    assert first["verification_result"]["attempt_count"] == 1

    second = verify_action(db_conn, action["id"], read_client=read_client)
    assert second["status"] == "VERIFIED_SUCCESS"
    assert second["verification_result"]["attempt_count"] == 2
    assert len(read_client.calls) == 2


def test_read_failures_exhausting_bound_escalates_exactly_once(db_conn, demo_merchant_id):
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_escalate", amount=10000)
    always_fails = SpyReadClient(lambda pid: (_ for _ in ()).throw(RazorpayAPIError("HTTP 503", status_code=503)))

    result = None
    for _ in range(MAX_READ_ATTEMPTS):
        result = verify_action(db_conn, action["id"], read_client=always_fails)

    assert result["status"] == "ESCALATED"
    assert len(always_fails.calls) == MAX_READ_ATTEMPTS
    assert result["verification_result"]["attempt_count"] == MAX_READ_ATTEMPTS

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(action["decision_id"]))]
    assert checkpoints.count("VERIFICATION_COMPLETED") == 1  # exactly one, not one per attempt


def test_unexpected_status_escalates_immediately_no_retry(db_conn, demo_merchant_id):
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_unexpected", amount=10000)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "refunded", "amount": 10000})

    result = verify_action(db_conn, action["id"], read_client=read_client)

    assert result["status"] == "ESCALATED"
    assert len(read_client.calls) == 1  # no retry attempted for an unexpected-but-successful read
    assert result["verification_result"]["attempt_count"] == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_reverifying_a_terminal_action_is_a_noop(db_conn, demo_merchant_id):
    action = _make_verifying_action(db_conn, demo_merchant_id, "order_ver_idempotent", amount=10000)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})

    first = verify_action(db_conn, action["id"], read_client=read_client)
    assert first["status"] == "VERIFIED_SUCCESS"
    assert len(read_client.calls) == 1

    second = verify_action(db_conn, action["id"], read_client=read_client)
    assert second["status"] == "VERIFIED_SUCCESS"
    assert len(read_client.calls) == 1  # NOT called again

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(action["decision_id"]))]
    assert checkpoints.count("VERIFICATION_COMPLETED") == 1


# ---------------------------------------------------------------------------
# Concurrency: the underlying row-lock primitive that verify_action()
# relies on. Two REAL separate connections, proving get_action_for_update
# actually blocks a concurrent transaction on the same row rather than
# merely happening to look safe in single-threaded tests.
#
# UNLIKE every other test in this suite, these two tests call
# db_conn.commit() mid-test -- a second, genuinely independent connection
# can only see a row once it's committed (Postgres MVCC isolation), so
# proving real cross-connection locking requires actually committing the
# setup data rather than relying on the fixture's end-of-test rollback.
# Consequently this setup data is NOT cleaned up afterward. Each order_id
# is suffixed with a fresh uuid so repeated test-suite runs never collide
# on the orders primary key -- a small, intentional, permanent footprint
# in the dev database, not an oversight.
# ---------------------------------------------------------------------------

def test_get_action_for_update_blocks_concurrent_transaction(db_conn, demo_merchant_id):
    order_id = f"order_ver_lock_{uuid.uuid4().hex[:8]}"
    action = _make_verifying_action(db_conn, demo_merchant_id, order_id, amount=10000)
    action_id = action["id"]
    db_conn.commit()  # ensure the action is visible to the second connection

    database_url = os.environ["DATABASE_URL"]
    conn2 = psycopg.connect(database_url)

    events: list[str] = []
    release_lock = threading.Event()

    def holder():
        with db_conn.transaction():
            get_action_for_update(db_conn, action_id)
            events.append("holder_acquired")
            release_lock.wait(timeout=5)
            events.append("holder_releasing")
        # transaction committed here -- lock released

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()

    # Give the holder a moment to acquire the lock first.
    time.sleep(0.3)
    assert "holder_acquired" in events

    try:
        with conn2.transaction():
            start = time.monotonic()
            get_action_for_update(conn2, action_id)  # must block until holder releases
            waited = time.monotonic() - start
            events.append("waiter_acquired")
    finally:
        conn2.close()

    release_lock.set()
    holder_thread.join(timeout=5)

    assert events == ["holder_acquired", "holder_releasing", "waiter_acquired"]
    assert waited >= 0.2  # genuinely waited for the holder, not a coincidence


def test_verify_action_serializes_across_two_connections(db_conn, demo_merchant_id):
    """End-to-end version of the lock test, through verify_action()
    itself rather than the raw primitive: two connections racing to
    verify the SAME action must not both write a terminal state /
    duplicate audit entry."""
    order_id = f"order_ver_concurrent_e2e_{uuid.uuid4().hex[:8]}"
    action = _make_verifying_action(db_conn, demo_merchant_id, order_id, amount=10000)
    action_id = action["id"]
    db_conn.commit()

    database_url = os.environ["DATABASE_URL"]
    conn2 = psycopg.connect(database_url)
    results: dict[str, Any] = {}

    def verify_on(conn, key, read_client):
        results[key] = verify_action(conn, action_id, read_client=read_client)

    read_client_1 = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})
    read_client_2 = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})

    t1 = threading.Thread(target=verify_on, args=(db_conn, "a", read_client_1))
    t2 = threading.Thread(target=verify_on, args=(conn2, "b", read_client_2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    conn2.close()

    assert results["a"]["status"] == "VERIFIED_SUCCESS"
    assert results["b"]["status"] == "VERIFIED_SUCCESS"
    # Only ONE of the two connections actually performed the read and
    # wrote the terminal state; the other saw it already resolved once
    # it acquired the lock and returned the existing row untouched.
    total_reads = len(read_client_1.calls) + len(read_client_2.calls)
    assert total_reads == 1

    checkpoints = [a["checkpoint"] for a in list_audit_trail_for_decision(db_conn, str(action["decision_id"]))]
    assert checkpoints.count("VERIFICATION_COMPLETED") == 1
