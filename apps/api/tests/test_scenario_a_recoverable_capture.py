"""Scenario A end-to-end: authorized payment -> recommended for capture
-> auto-allowed by policy -> captured -> verified success.

Composes ONLY existing production modules, in the exact sequence a real
workflow would call them:

    reconciliation.service.reconcile_order
    context.builder.build_context_snapshot
    intelligence.expectation (zero-evidence default, constructed the same
        way intelligence.orchestration.make_decision does for a context
        with no error_reason to bucket on)
    repository.decisions.insert_decision + repository.audit.insert_audit_entry
        (DECISION_CREATED) -- decision_type is hand-supplied
        (RECOMMEND_CAPTURE), not produced by RuleBasedEngine. RuleBasedEngine
        cannot recommend a capture today: ContextSnapshot for a
        payment-attempt context doesn't carry the payment's own status,
        and RuleBasedEngine never sees the triggering event_type either --
        closing that gap would mean modifying the already-committed
        Context (Gate 5) and RuleBasedEngine (Gate 6) modules, which was
        explicitly ruled out of scope for this assembly gate. This is a
        stated capability gap, not a workaround.
    action.orchestrator.propose_action -> real Policy + Action + capture
    verification.verifier.verify_action -> real Verification

No production src/ code was added or modified for this gate.
"""

from __future__ import annotations

from context.builder import build_context_snapshot
from domain.contracts import Expectation
from intelligence.expectation import ZERO_EVIDENCE_SOURCE
from reconciliation.service import reconcile_order
from repository.audit import insert_audit_entry
from repository.canonical_events import list_events_for_order
from action.orchestrator import propose_action
from support import (
    FakeReconciliationClient,
    SpyReadClient,
    SpyWriteClient,
    full_audit_trail,
    insert_capture_decision,
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

    # ---- 2. Context (real production call) ----
    context = build_context_snapshot(db_conn, authorized_event)
    assert context.payment_attempt_id == payment_id
    amount_field = next(f for f in context.fields if f.field == "amount")
    assert amount_field.value == amount

    # ---- 3. Expectation: zero-evidence default -- no error_reason exists
    #         to bucket on for an authorized (non-failure) context, same
    #         logic intelligence.orchestration.make_decision applies.
    expectation = Expectation(
        bucket_key="no_error_reason", expected_recovery_rate=0.5, sample_size=0, source=ZERO_EVIDENCE_SOURCE
    )
    assert expectation.source == ZERO_EVIDENCE_SOURCE

    # ---- 4. Decision: hand-supplied RECOMMEND_CAPTURE (see module docstring) ----
    decision_id = insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=amount)
    # insert_capture_decision() is a shared helper also used by
    # non-scenario tests whose assertions don't expect a DECISION_CREATED
    # entry -- writing it there would silently change their behavior.
    # This scenario's own audit trail (per the approved design) does
    # expect one, mirroring what intelligence.orchestration.make_decision
    # would write, so it's added explicitly here instead.
    insert_audit_entry(
        db_conn, "DECISION_CREATED", {"decision_type": "RECOMMEND_CAPTURE"},
        event_id=str(authorized_event["id"]), decision_id=str(decision_id),
    )

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
