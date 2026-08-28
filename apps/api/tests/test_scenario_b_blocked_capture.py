"""Scenario B end-to-end: authorized payment -> recommended for capture
-> policy blocks it for exceeding the merchant's configured hard limit
-> zero Razorpay write calls.

Same merchant, same policy_config as Scenario A -- only the amount
differs. This deliberately demonstrates "the same recommendation, a
different policy verdict" purely as a function of amount, not a
different merchant configuration: RuleBasedEngine has no amount-based
gating (that's Policy's exclusive job), so both scenarios receive the
SAME RECOMMEND_CAPTURE recommendation from the SAME engine call, and
only Policy's verdict diverges.

Composes ONLY existing production modules -- see
test_scenario_a_recoverable_capture.py's module docstring for the full
reasoning on the RuleBasedEngine capture rule this now genuinely
exercises (no hand-constructed Decision remains in either scenario).
"""

from __future__ import annotations

from action.orchestrator import propose_action
from intelligence.orchestration import make_decision
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from support import FakeReconciliationClient, SpyWriteClient, full_audit_trail, set_policy_config


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

    # ---- 2-4. Context + Expectation + RuleBasedEngine + Decision
    #           persistence, all real production code. RuleBasedEngine
    #           recommends capture regardless of amount -- SAME
    #           recommendation as Scenario A. ----
    decision_id = make_decision(db_conn, demo_merchant_id, authorized_event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "RECOMMEND_CAPTURE"
    assert float(decision["confidence"]) == 1.0
    assert decision["reason_codes"] == ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"]
    assert decision["model_version"] == "rule_v1"

    # ---- 5. Policy blocks; Action never reaches EXECUTING (real production call) ----
    # SAME policy_config as Scenario A -- only the amount differs. This is
    # where the two scenarios' outcomes actually diverge: Policy, not
    # RuleBasedEngine.
    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    write_spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=write_spy)

    # ---- Pass/fail assertions ----
    assert action["status"] == "BLOCKED"
    assert write_spy.calls == []  # zero Razorpay write calls, the single most important assertion here
    assert "AMOUNT_EXCEEDS_HARD_LIMIT" in action["policy_evaluation"]["reason_codes"]

    checkpoints = full_audit_trail(db_conn, str(authorized_event["id"]), str(decision_id))
    assert checkpoints == ["EVENT_INGESTED", "DECISION_CREATED", "POLICY_EVALUATED", "ACTION_BLOCKED"]
