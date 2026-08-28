"""Shared test-only helpers. Not a test module itself (no test_ prefix,
not collected by pytest) -- imported by test files that need it.

insert_capture_decision() was previously duplicated verbatim across
test_action_orchestration.py, test_policy_orchestration.py, and
test_verification.py. Consolidated here with identical behavior -- no
production code touched, no test behavior changed.
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

import psycopg

from repository.canonical_events import list_events_for_order
from repository.decisions import insert_decision


def insert_capture_decision(
    conn: psycopg.Connection, merchant_id: str, order_id: str, payment_attempt_id: str, amount: int
) -> UUID:
    """Hand-constructed RECOMMEND_CAPTURE Decision -- RuleBasedEngine
    does not produce this decision_type (approved gate constraint since
    Gate 7: Policy/Action/Verification are deliberately tested in
    isolation from Intelligence for this decision type). Not a claim
    that this flow runs through RuleBasedEngine end-to-end."""
    return insert_decision(
        conn, merchant_id, order_id, payment_attempt_id,
        str(list_events_for_order(conn, order_id)[0]["id"]),
        {"fields": []}, {"bucket_key": "n/a", "expected_recovery_rate": 1.0, "sample_size": 0, "source": "test_fixture"},
        "RECOMMEND_CAPTURE", 1.0, ["TEST_FIXTURE_CAPTURE_RECOMMENDATION"],
        {"revenue_at_stake": amount}, "test_fixture",
    )


def full_audit_trail(conn: psycopg.Connection, event_id: str, decision_id: str) -> list[str]:
    """The complete chronological checkpoint sequence for one flow,
    combining both audit_entries columns that scope to it: EVENT_INGESTED
    is recorded against event_id, everything from DECISION_CREATED onward
    against decision_id.

    Ordered by sequence_number, NOT created_at: created_at defaults to
    Postgres's now(), which is fixed for the whole enclosing transaction
    (every audit row inserted within one test shares the identical
    value), so it cannot discriminate insertion order at all -- ties were
    silently broken by query-plan-dependent order, which happened to
    look right for a single-column WHERE and did not for this OR-combined
    one. sequence_number is a bigint identity column added specifically
    to fix this (0004_audit_entries_ordering_sequence.sql)."""
    with conn.cursor() as cur:
        cur.execute(
            "select checkpoint from audit_entries where event_id = %s or decision_id = %s order by sequence_number asc",
            (event_id, decision_id),
        )
        return [row[0] for row in cur.fetchall()]


def set_policy_config(conn: psycopg.Connection, merchant_id: str, config: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update merchants set policy_config = %s where id = %s",
            (psycopg.types.json.Jsonb(config), merchant_id),
        )


class SpyWriteClient:
    """Test double for action.razorpay_write_client.RazorpayWriteClient.
    Records every call; the configured responder decides the outcome."""

    def __init__(self, responder: Callable[[str, int], dict[str, Any]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or (lambda pid, amt: {"id": pid, "status": "captured", "captured": True})

    def capture_payment(self, payment_id: str, amount: int, currency: str = "INR") -> dict[str, Any]:
        self.calls.append({"payment_id": payment_id, "amount": amount, "currency": currency})
        return self._responder(payment_id, amount)


class SpyReadClient:
    """Test double for razorpay_client.client.RazorpayReadClientProtocol.
    Only get_payment is used by verification.verifier.verify_action()."""

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


class FakeReconciliationClient:
    """Deterministic RazorpayReadClientProtocol double for reconciliation --
    models the VERIFIED Phase 1 manual-capture order shape
    (order_TV4zd1gEZHQRZ7 / pay_TV530e8hTSjpC8): payment_capture:0,
    landing in `authorized`, not auto-captured."""

    def __init__(self, order_id: str, payment_id: str, amount: int):
        self._order = {
            "id": order_id, "amount": amount, "amount_paid": 0, "amount_due": amount,
            "currency": "INR", "status": "created", "attempts": 1,
        }
        self._payment = {
            "id": payment_id, "order_id": order_id, "status": "authorized", "amount": amount,
            "method": "card", "captured": False,
            "error_source": None, "error_step": None, "error_reason": None,
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [dict(self._payment)]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return dict(self._payment)
