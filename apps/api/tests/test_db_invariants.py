"""Database-invariant tests. Requires a live PostgreSQL instance reachable
via DATABASE_URL with migrations applied (python -m db.run_migrations).

BLOCKED in the current environment: no Docker and no local Postgres
install exist here (see the Phase 3 gate report). These tests are fully
authored and ready -- they have not been executed yet. Run with:

    export DATABASE_URL=postgresql://user:pass@host:5432/dbname
    python -m db.run_migrations
    pytest tests/test_db_invariants.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from domain.contracts import ActionType, compute_idempotency_key
from repository.actions import DuplicateAction, get_action, insert_action
from repository.audit import insert_audit_entry, list_audit_trail_for_decision
from repository.canonical_events import insert_canonical_event, list_events_for_order
from repository.decisions import insert_decision
from repository.orders import upsert_order
from repository.payment_attempts import (
    InvalidPaymentAttemptTransition,
    insert_payment_attempt,
    update_payment_attempt_status,
    get_payment_attempt,
)


def _make_order(conn, merchant_id: str, order_id: str, **overrides) -> str:
    defaults = dict(
        amount=50000, amount_paid=0, amount_due=50000, status="created",
        attempts=0, currency="INR", raw_reference={"id": order_id},
    )
    defaults.update(overrides)
    upsert_order(conn, order_id, merchant_id, raw_reference=defaults.pop("raw_reference"), **defaults)
    return order_id


# ---------------------------------------------------------------------------
# 1. Multiple payment attempts under one order
# ---------------------------------------------------------------------------

def test_multiple_payment_attempts_under_one_order(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_multi")

    insert_payment_attempt(db_conn, "pay_test_1", order_id, "failed", "card", False,
                            "customer", "payment_authentication", "payment_cancelled", 50000, {})
    insert_payment_attempt(db_conn, "pay_test_2", order_id, "failed", "card", False,
                            "gateway", "payment_authorization", "payment_failed", 50000, {})
    insert_payment_attempt(db_conn, "pay_test_3", order_id, "captured", "card", True,
                            None, None, None, 50000, {})

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from payment_attempts where order_id = %s", (order_id,))
        assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 2. Failed attempt followed by a successful NEW payment attempt
#    (distinct pay_ ids, never a mutation of the failed one)
# ---------------------------------------------------------------------------

def test_failed_then_new_successful_attempt_are_distinct_rows(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_retry")
    insert_payment_attempt(db_conn, "pay_retry_1", order_id, "failed", "card", False,
                            "gateway", "payment_authorization", "payment_failed", 50000, {})
    insert_payment_attempt(db_conn, "pay_retry_2", order_id, "captured", "card", True,
                            None, None, None, 50000, {})

    first = get_payment_attempt(db_conn, "pay_retry_1")
    second = get_payment_attempt(db_conn, "pay_retry_2")
    assert first["id"] != second["id"]
    assert first["status"] == "failed"
    assert second["status"] == "captured"


# ---------------------------------------------------------------------------
# 3. Authorized -> captured (in-place transition on the SAME id)
# ---------------------------------------------------------------------------

def test_authorized_to_captured_transition(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_capture", status="created")
    insert_payment_attempt(db_conn, "pay_capture_1", order_id, "authorized", "card", False,
                            None, None, None, 50000, {})

    update_payment_attempt_status(db_conn, "pay_capture_1", "captured", True, {"captured": True})

    row = get_payment_attempt(db_conn, "pay_capture_1")
    assert row["status"] == "captured"
    assert row["captured"] is True


# ---------------------------------------------------------------------------
# 4. Invalid PaymentAttempt state transition is rejected, not overwritten
# ---------------------------------------------------------------------------

def test_invalid_transition_is_rejected(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_invalid")
    insert_payment_attempt(db_conn, "pay_invalid_1", order_id, "captured", "card", True,
                            None, None, None, 50000, {})

    with pytest.raises(InvalidPaymentAttemptTransition):
        update_payment_attempt_status(db_conn, "pay_invalid_1", "failed", False, {})

    # state must be unchanged after the rejected write
    row = get_payment_attempt(db_conn, "pay_invalid_1")
    assert row["status"] == "captured"


# ---------------------------------------------------------------------------
# 5. Duplicate action idempotency
# ---------------------------------------------------------------------------

def test_duplicate_action_idempotency_rejected(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_idem")
    insert_payment_attempt(db_conn, "pay_idem_1", order_id, "authorized", "card", False,
                            None, None, None, 50000, {})
    event_id = insert_canonical_event(
        db_conn, demo_merchant_id, "payment.attempt.authorized", "razorpay_api_poll",
        "payment", "pay_idem_1", order_id, datetime.now(timezone.utc), {},
    )
    decision_id = insert_decision(
        db_conn, demo_merchant_id, order_id, "pay_idem_1", str(event_id),
        {"fields": []}, {"bucket_key": "x", "expected_recovery_rate": 0.5, "sample_size": 1, "source": "rule_v1"},
        "RECOMMEND_CAPTURE", 0.8, ["TEST"], {}, "rule_v1",
    )
    key = compute_idempotency_key(demo_merchant_id, order_id, "pay_idem_1", ActionType.CAPTURE_PAYMENT)

    insert_action(db_conn, str(decision_id), key, "CAPTURE_PAYMENT", {"allowed": True}, "AUTHORIZED")

    with pytest.raises(DuplicateAction):
        insert_action(db_conn, str(decision_id), key, "CAPTURE_PAYMENT", {"allowed": True}, "AUTHORIZED")


# ---------------------------------------------------------------------------
# 11. Audit entries generated at required checkpoints
# ---------------------------------------------------------------------------

def test_audit_trail_records_checkpoints_in_order(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_audit")
    event_id = insert_canonical_event(
        db_conn, demo_merchant_id, "payment.attempt.failed", "razorpay_api_poll",
        "payment", "pay_audit_1", order_id, datetime.now(timezone.utc), {},
    )
    decision_id = insert_decision(
        db_conn, demo_merchant_id, order_id, "pay_audit_1", str(event_id),
        {"fields": []}, {"bucket_key": "x", "expected_recovery_rate": 0.5, "sample_size": 1, "source": "rule_v1"},
        "RECOMMEND_RETRY_PROMPT", 0.7, ["TEST"], {}, "rule_v1",
    )
    insert_audit_entry(db_conn, "DECISION_CREATED", {"decision_id": str(decision_id)}, event_id=str(event_id), decision_id=str(decision_id))
    insert_audit_entry(db_conn, "POLICY_EVALUATED", {"allowed": True}, decision_id=str(decision_id))

    trail = list_audit_trail_for_decision(db_conn, str(decision_id))
    assert [row["checkpoint"] for row in trail] == ["DECISION_CREATED", "POLICY_EVALUATED"]


def test_audit_entries_are_append_only(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_appendonly")
    event_id = insert_canonical_event(
        db_conn, demo_merchant_id, "order.paid", "razorpay_api_poll",
        "order", order_id, order_id, datetime.now(timezone.utc), {},
    )
    audit_id = insert_audit_entry(db_conn, "EVENT_INGESTED", {"note": "test"}, event_id=str(event_id))

    with pytest.raises(psycopg.errors.RaiseException):
        with db_conn.cursor() as cur:
            cur.execute("update audit_entries set checkpoint = 'ACTION_BLOCKED' where id = %s", (audit_id,))


# ---------------------------------------------------------------------------
# 13. Decision -> Policy -> Action lineage (DB linkage, complements the
#     pure-Python idempotency-key test in test_domain_contracts.py)
# ---------------------------------------------------------------------------

def test_action_references_its_decision(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_lineage")
    event_id = insert_canonical_event(
        db_conn, demo_merchant_id, "payment.attempt.authorized", "razorpay_api_poll",
        "payment", "pay_lineage_1", order_id, datetime.now(timezone.utc), {},
    )
    decision_id = insert_decision(
        db_conn, demo_merchant_id, order_id, "pay_lineage_1", str(event_id),
        {"fields": []}, {"bucket_key": "x", "expected_recovery_rate": 0.5, "sample_size": 1, "source": "rule_v1"},
        "RECOMMEND_CAPTURE", 0.8, ["TEST"], {}, "rule_v1",
    )
    key = compute_idempotency_key(demo_merchant_id, order_id, "pay_lineage_1", ActionType.CAPTURE_PAYMENT)
    action_id = insert_action(db_conn, str(decision_id), key, "CAPTURE_PAYMENT", {"allowed": True}, "AUTHORIZED")

    action = get_action(db_conn, action_id)
    assert str(action["decision_id"]) == str(decision_id)


# ---------------------------------------------------------------------------
# 14. Reconciliation is idempotent -- re-observing the same status is a no-op
# ---------------------------------------------------------------------------

def test_reobserving_same_status_is_a_noop(db_conn, demo_merchant_id):
    order_id = _make_order(db_conn, demo_merchant_id, "order_test_noop")
    insert_payment_attempt(db_conn, "pay_noop_1", order_id, "authorized", "card", False,
                            None, None, None, 50000, {})

    # same status re-applied twice must not raise, and must not be treated
    # as an invalid transition by the guard trigger (old == new short-circuits)
    update_payment_attempt_status(db_conn, "pay_noop_1", "authorized", False, {})
    update_payment_attempt_status(db_conn, "pay_noop_1", "authorized", False, {})

    row = get_payment_attempt(db_conn, "pay_noop_1")
    assert row["status"] == "authorized"
