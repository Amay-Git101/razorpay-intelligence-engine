"""Scenario B end-to-end: authorized payment -> recommended for capture
-> policy blocks it for exceeding the merchant's configured hard limit
-> zero Razorpay write calls.

Same merchant, same policy_config as Scenario A -- only the amount
differs. This deliberately demonstrates "the same recommendation, a
different policy verdict" purely as a function of amount, not a
different merchant configuration.

Composes ONLY existing production modules -- see
test_scenario_a_recoverable_capture.py's module docstring for the full
reasoning on why the Decision here is hand-supplied rather than produced
by RuleBasedEngine (an explicitly out-of-scope capability gap for this
gate, not a workaround). No production src/ code was added or modified.
"""

from __future__ import annotations

from action.orchestrator import propose_action
from context.builder import build_context_snapshot
from domain.contracts import Expectation
from intelligence.expectation import ZERO_EVIDENCE_SOURCE
from reconciliation.service import reconcile_order
from repository.audit import insert_audit_entry
from repository.canonical_events import list_events_for_order
from support import (
    FakeReconciliationClient,
    SpyWriteClient,
    full_audit_trail,
    insert_capture_decision,
    set_policy_config,
)


def test_scenario_b_blocked_capture_makes_zero_razorpay_calls(db_conn, demo_merchant_id):
    order_id = "order_scenario_b"
    payment_id = f"pay_{order_id}"
    amount = 500000  # comfortably above the hard limit configured below

    # ---- 1. Razorpay observation + reconciliation (real production call) ----
    reconciliation_client = FakeReconciliationClient(order_id, payment_id, amount)
    new_event_ids = reconcile_order(db_conn, reconciliation_client, demo_merchant_id, order_id)
    assert len(new_event_ids) == 2  # order.created + payment.attempt.authorized

    events = list_events_for_order(db_conn, order_id)
    authorized_event = next(e for e in events if e["event_type"] == "payment.attempt.authorized")

    # ---- 2. Context (real production call) ----
    context = build_context_snapshot(db_conn, authorized_event)
    amount_field = next(f for f in context.fields if f.field == "amount")
    assert amount_field.value == amount

    # ---- 3. Expectation: zero-evidence default (no error_reason to bucket on) ----
    expectation = Expectation(
        bucket_key="no_error_reason", expected_recovery_rate=0.5, sample_size=0, source=ZERO_EVIDENCE_SOURCE
    )
    assert expectation.source == ZERO_EVIDENCE_SOURCE

    # ---- 4. Decision: hand-supplied RECOMMEND_CAPTURE ----
    decision_id = insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=amount)
    # See test_scenario_a_recoverable_capture.py for why this is written
    # here rather than inside the shared insert_capture_decision() helper.
    insert_audit_entry(
        db_conn, "DECISION_CREATED", {"decision_type": "RECOMMEND_CAPTURE"},
        event_id=str(authorized_event["id"]), decision_id=str(decision_id),
    )

    # ---- 5. Policy blocks; Action never reaches EXECUTING (real production call) ----
    # SAME policy_config as Scenario A -- only the amount differs.
    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    write_spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=write_spy)

    # ---- Pass/fail assertions ----
    assert action["status"] == "BLOCKED"
    assert write_spy.calls == []  # zero Razorpay write calls, the single most important assertion here
    assert "AMOUNT_EXCEEDS_HARD_LIMIT" in action["policy_evaluation"]["reason_codes"]

    checkpoints = full_audit_trail(db_conn, str(authorized_event["id"]), str(decision_id))
    assert checkpoints == ["EVENT_INGESTED", "DECISION_CREATED", "POLICY_EVALUATED", "ACTION_BLOCKED"]
