"""Scenario A end-to-end: authorized payment -> recommended for capture
-> auto-allowed by policy -> captured -> verified success.

Composes ONLY existing production modules, in the exact sequence a real
workflow would call them:

    reconciliation.service.reconcile_order
    intelligence.orchestration.make_decision
        (internally: context.builder.build_context_snapshot ->
         intelligence.expectation -> RuleBasedEngine.evaluate ->
         repository.decisions.insert_decision -> DECISION_CREATED audit)
    action.orchestrator.propose_action -> real Policy + Action + capture
    verification.verifier.verify_action -> real Verification

The Decision is now GENUINELY produced by RuleBasedEngine -- this test
previously used a hand-constructed RECOMMEND_CAPTURE Decision because
RuleBasedEngine had no rule for it. That gap is closed (context/builder.py
and intelligence/rule_based.py both gained a small, explicit addition:
a required `status` RAW field and a rule that fires only when
status == "authorized"). See those modules for the full reasoning.
"""

from __future__ import annotations

from action.orchestrator import propose_action
from intelligence.orchestration import make_decision
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from support import (
    FakeReconciliationClient,
    SpyReadClient,
    SpyWriteClient,
    full_audit_trail,
    set_policy_config,
)
from verification.verifier import verify_action


def test_scenario_a_recoverable_capture_ends_verified_success(db_conn, demo_merchant_id):
    order_id = "order_scenario_a"
    payment_id = f"pay_{order_id}"
    amount = 10000  # comfortably inside the auto-allow band configured below

    # ---- 1. Razorpay observation + reconciliation (real production call) ----
    reconciliation_client = FakeReconciliationClient(order_id, payment_id, amount)
    new_event_ids = reconcile_order(db_conn, reconciliation_client, demo_merchant_id, order_id)
    assert len(new_event_ids) == 2  # order.created + payment.attempt.authorized

    events = list_events_for_order(db_conn, order_id)
    authorized_event = next(e for e in events if e["event_type"] == "payment.attempt.authorized")

    # ---- 2-4. Context + Expectation + RuleBasedEngine + Decision
    #           persistence, all real production code, no hand-supplied
    #           decision_type. ----
    decision_id = make_decision(db_conn, demo_merchant_id, authorized_event)

    decision = get_decision(db_conn, decision_id)
    assert decision["decision_type"] == "RECOMMEND_CAPTURE"
    assert float(decision["confidence"]) == 1.0
    assert decision["reason_codes"] == ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"]
    assert decision["model_version"] == "rule_v1"  # genuinely engine-produced, not "test_fixture"
    assert decision["context_snapshot"]["payment_attempt_id"] == payment_id
    status_field = next(f for f in decision["context_snapshot"]["fields"] if f["field"] == "status")
    assert status_field["value"] == "authorized"

    # ---- 5. Policy + Action + capture, auto-allowed (real production call) ----
    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    write_spy = SpyWriteClient()
    action = propose_action(db_conn, decision_id, write_client=write_spy)

    assert action["status"] == "VERIFYING"
    assert len(write_spy.calls) == 1
    assert write_spy.calls[0] == {"payment_id": payment_id, "amount": amount, "currency": "INR"}

    # ---- 6. Verification (real production call) ----
    read_spy = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": amount})
    final_action = verify_action(db_conn, action["id"], read_client=read_spy)

    # ---- Pass/fail assertions ----
    assert final_action["status"] == "VERIFIED_SUCCESS"
    assert final_action["outcome"]["recovered_amount"] == amount
    assert "verified_at" in final_action["outcome"]
    assert "time_to_resolution_seconds" in final_action["outcome"]
    assert len(read_spy.calls) == 1  # exactly one verification read

    checkpoints = full_audit_trail(db_conn, str(authorized_event["id"]), str(decision_id))
    assert checkpoints == [
        "EVENT_INGESTED", "DECISION_CREATED", "POLICY_EVALUATED",
        "ACTION_AUTHORIZED", "ACTION_EXECUTED", "VERIFICATION_COMPLETED",
    ]

    # No anomalies anywhere in this run.
    with db_conn.cursor() as cur:
        cur.execute(
            "select count(*) from canonical_events where order_id = %s and event_type = %s",
            (order_id, "payment.attempt.anomaly"),
        )
        assert cur.fetchone()[0] == 0
