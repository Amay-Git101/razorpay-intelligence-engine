"""Observability metrics DB-integration tests. Requires live Postgres.

Builds small, controlled datasets via the real production pipeline
(reconcile_order -> make_decision -> propose_action -> verify_action) --
the same pattern already used by test_scenario_a_recoverable_capture.py
and test_verification.py -- never hand-crafts decisions/actions rows
directly. No CUSTOMER_RETRY_PROMPT outcome is ever fabricated: that
action_type is only ever driven to its real terminal state (AUTHORIZED,
per action/orchestrator.py's documented scope), never further.
"""

from __future__ import annotations

from typing import Any, Callable

import psycopg
import pytest

from action.orchestrator import propose_action
from intelligence.orchestration import make_decision
from observability.metrics import (
    CaptureTerminalStatusDistribution,
    DecisionTypeDistribution,
    EscalationMetrics,
    PolicyOutcomeDistribution,
    RetryPromptOutcomeAvailability,
    VerificationReadAttemptDistribution,
    VerificationResolutionTiming,
    VerifiedCapturedAmount,
    capture_terminal_status_distribution,
    decision_type_distribution,
    escalation_metrics,
    policy_outcome_distribution,
    retry_prompt_outcome_availability,
    verification_read_attempt_distribution,
    verification_resolution_timing,
    verified_captured_amount,
)
from razorpay_client.errors import RazorpayAPIError
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.merchants import insert_merchant
from support import FakeReconciliationClient, SpyReadClient, SpyWriteClient, set_policy_config
from verification.verifier import verify_action

# ---------------------------------------------------------------------------
# Fixture builders -- real Decision/Action/Verification rows via the
# actual production pipeline.
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, order: dict[str, Any], payments: list[dict[str, Any]]):
        self._order = order
        self._payments = payments

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._order)

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


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


def _decide_for_event(conn: psycopg.Connection, merchant_id: str, order_id: str, event_type: str) -> str:
    events = list_events_for_order(conn, order_id)
    event = next(e for e in events if e["event_type"] == event_type)
    return str(make_decision(conn, merchant_id, event))


def _capture_decision(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int) -> str:
    payment_id = f"pay_{order_id}"
    client = FakeReconciliationClient(order_id, payment_id, amount)
    reconcile_order(conn, client, merchant_id, order_id)
    return _decide_for_event(conn, merchant_id, order_id, "payment.attempt.authorized")


def _retry_prompt_decision(conn: psycopg.Connection, merchant_id: str, order_id: str) -> str:
    client = _FakeClient(
        order=_order_fixture(order_id, "created", 0, 50000, 1),
        payments=[_payment_fixture(
            f"pay_{order_id}", order_id, "failed", False,
            error_source="gateway", error_step="payment_authorization", error_reason="payment_failed",
        )],
    )
    reconcile_order(conn, client, merchant_id, order_id)
    return _decide_for_event(conn, merchant_id, order_id, "payment.attempt.failed")


def _blocked_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 500000) -> dict[str, Any]:
    decision_id = _capture_decision(conn, merchant_id, order_id, amount)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["status"] == "BLOCKED"
    return action


def _approval_required_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 50000) -> dict[str, Any]:
    decision_id = _capture_decision(conn, merchant_id, order_id, amount)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["status"] == "APPROVAL_PENDING"
    return action


def _verifying_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    decision_id = _capture_decision(conn, merchant_id, order_id, amount)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["status"] == "VERIFYING"
    return action


def _verified_success_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "VERIFIED_SUCCESS"
    return final


def _verified_failed_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "authorized", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "VERIFIED_FAILED"
    return final


def _escalated_unexpected_status_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "refunded", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "ESCALATED"
    assert final["verification_result"]["reason"] == "UNEXPECTED_PAYMENT_STATUS:refunded"
    return final


def _escalated_read_failure_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount)
    always_fails = SpyReadClient(lambda pid: (_ for _ in ()).throw(RazorpayAPIError("HTTP 503", status_code=503)))
    final = None
    for _ in range(3):
        final = verify_action(conn, action["id"], read_client=always_fails)
    assert final["status"] == "ESCALATED"
    assert final["verification_result"]["reason"] == "VERIFICATION_READ_FAILED_BOUND_EXHAUSTED"
    return final


def _sequence_responder(*outcomes: Any) -> Callable[[str], dict[str, Any]]:
    it = iter(outcomes)

    def responder(payment_id: str) -> dict[str, Any]:
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return responder


def _retry_then_succeed_capture(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int = 10000) -> dict[str, Any]:
    """attempt_count ends at exactly 2: one read failure, then a successful read."""
    action = _verifying_capture(conn, merchant_id, order_id, amount)
    read_client = SpyReadClient(_sequence_responder(
        RazorpayAPIError("HTTP 503", status_code=503),
        {"id": "x", "status": "captured", "amount": amount},
    ))
    verify_action(conn, action["id"], read_client=read_client)
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "VERIFIED_SUCCESS"
    assert final["verification_result"]["attempt_count"] == 2
    return final


def _second_merchant(conn: psycopg.Connection) -> str:
    return str(insert_merchant(conn, "Second Merchant", {}, {}))


# ---------------------------------------------------------------------------
# 1. Decision-type distribution
# ---------------------------------------------------------------------------

def test_decision_type_distribution_counts_and_merchant_isolation(db_conn, demo_merchant_id):
    other_merchant_id = _second_merchant(db_conn)

    _capture_decision(db_conn, demo_merchant_id, "order_dt_a_capture", 10000)
    _retry_prompt_decision(db_conn, demo_merchant_id, "order_dt_a_retry1")
    _retry_prompt_decision(db_conn, demo_merchant_id, "order_dt_a_retry2")

    client = _FakeClient(
        order=_order_fixture("order_dt_a_noaction", "created", 0, 50000, 1),
        payments=[_payment_fixture(
            "pay_dt_a_noaction", "order_dt_a_noaction", "failed", False,
            error_source="customer", error_step="payment_authentication", error_reason="payment_cancelled",
        )],
    )
    reconcile_order(db_conn, client, demo_merchant_id, "order_dt_a_noaction")
    _decide_for_event(db_conn, demo_merchant_id, "order_dt_a_noaction", "payment.attempt.failed")

    # Merchant B's row must never leak into merchant A's distribution.
    _capture_decision(db_conn, other_merchant_id, "order_dt_b_capture", 10000)

    result = decision_type_distribution(db_conn, demo_merchant_id)

    assert isinstance(result, DecisionTypeDistribution)
    assert result.merchant_id == demo_merchant_id
    assert result.counts == {"NO_ACTION": 1, "RECOMMEND_CAPTURE": 1, "RECOMMEND_RETRY_PROMPT": 2}

    other_result = decision_type_distribution(db_conn, other_merchant_id)
    assert other_result.counts == {"RECOMMEND_CAPTURE": 1}


# ---------------------------------------------------------------------------
# 2. Policy-outcome distribution
# ---------------------------------------------------------------------------

def test_policy_outcome_distribution_separates_allow_approval_block(db_conn, demo_merchant_id):
    _verifying_capture(db_conn, demo_merchant_id, "order_pd_allow1", amount=10000)
    _verifying_capture(db_conn, demo_merchant_id, "order_pd_allow2", amount=15000)
    _approval_required_capture(db_conn, demo_merchant_id, "order_pd_approval", amount=50000)
    _blocked_capture(db_conn, demo_merchant_id, "order_pd_block", amount=500000)

    result = policy_outcome_distribution(db_conn, demo_merchant_id)

    assert isinstance(result, PolicyOutcomeDistribution)
    assert result.allow == 2
    assert result.approval_required == 1
    assert result.block == 1


# ---------------------------------------------------------------------------
# 3. Capture terminal-status distribution
# ---------------------------------------------------------------------------

def test_capture_terminal_status_distribution_counts_and_excludes_retry_prompt(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_ct_success")
    _verified_failed_capture(db_conn, demo_merchant_id, "order_ct_failed")
    _escalated_unexpected_status_capture(db_conn, demo_merchant_id, "order_ct_escalated")
    _blocked_capture(db_conn, demo_merchant_id, "order_ct_blocked")

    # A CUSTOMER_RETRY_PROMPT action for the same merchant must never
    # appear in this distribution -- it isn't a capture at all.
    retry_decision_id = _retry_prompt_decision(db_conn, demo_merchant_id, "order_ct_retry")
    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    retry_action = propose_action(db_conn, retry_decision_id, write_client=SpyWriteClient())
    assert retry_action["action_type"] == "CUSTOMER_RETRY_PROMPT"

    result = capture_terminal_status_distribution(db_conn, demo_merchant_id)

    assert isinstance(result, CaptureTerminalStatusDistribution)
    assert result.verified_success == 1
    assert result.verified_failed == 1
    assert result.escalated == 1
    assert result.blocked == 1


# ---------------------------------------------------------------------------
# 4. Escalation metrics
# ---------------------------------------------------------------------------

def test_escalation_metrics_breaks_down_by_reason(db_conn, demo_merchant_id):
    _escalated_unexpected_status_capture(db_conn, demo_merchant_id, "order_esc_unexpected1")
    _escalated_unexpected_status_capture(db_conn, demo_merchant_id, "order_esc_unexpected2")
    _escalated_read_failure_capture(db_conn, demo_merchant_id, "order_esc_readfail")
    _verified_success_capture(db_conn, demo_merchant_id, "order_esc_success")  # must not be counted

    result = escalation_metrics(db_conn, demo_merchant_id)

    assert isinstance(result, EscalationMetrics)
    assert result.total_escalated == 3
    assert result.by_reason == {
        "UNEXPECTED_PAYMENT_STATUS:refunded": 2,
        "VERIFICATION_READ_FAILED_BOUND_EXHAUSTED": 1,
    }


# ---------------------------------------------------------------------------
# 5. Verification read-attempt distribution
# ---------------------------------------------------------------------------

def test_verification_read_attempt_distribution_groups_by_attempt_count(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_ra_one")  # attempt_count == 1
    _retry_then_succeed_capture(db_conn, demo_merchant_id, "order_ra_two")  # attempt_count == 2
    _escalated_read_failure_capture(db_conn, demo_merchant_id, "order_ra_three")  # attempt_count == 3
    _blocked_capture(db_conn, demo_merchant_id, "order_ra_never_verified")  # never entered Verification

    result = verification_read_attempt_distribution(db_conn, demo_merchant_id)

    assert isinstance(result, VerificationReadAttemptDistribution)
    assert result.by_attempt_count == {1: 1, 2: 1, 3: 1}


# ---------------------------------------------------------------------------
# 6. Verified captured amount
# ---------------------------------------------------------------------------

def test_verified_captured_amount_sums_only_verified_success(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_vca_1", amount=10000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_vca_2", amount=25000)
    _verified_failed_capture(db_conn, demo_merchant_id, "order_vca_failed", amount=99999)  # must not contribute
    _blocked_capture(db_conn, demo_merchant_id, "order_vca_blocked", amount=500000)  # must not contribute

    result = verified_captured_amount(db_conn, demo_merchant_id)

    assert isinstance(result, VerifiedCapturedAmount)
    assert result.verified_success_count == 2
    assert result.total_verified_captured_amount == 35000


# ---------------------------------------------------------------------------
# 7. Verification resolution timing
# ---------------------------------------------------------------------------

def test_verification_resolution_timing_uses_persisted_outcome_values(db_conn, demo_merchant_id):
    action1 = _verified_success_capture(db_conn, demo_merchant_id, "order_rt_1")
    action2 = _verified_success_capture(db_conn, demo_merchant_id, "order_rt_2")
    _blocked_capture(db_conn, demo_merchant_id, "order_rt_blocked")  # no outcome at all -- must not affect timing

    t1 = action1["outcome"]["time_to_resolution_seconds"]
    t2 = action2["outcome"]["time_to_resolution_seconds"]

    result = verification_resolution_timing(db_conn, demo_merchant_id)

    assert isinstance(result, VerificationResolutionTiming)
    assert result.count == 2
    assert result.min_seconds == pytest.approx(min(t1, t2))
    assert result.max_seconds == pytest.approx(max(t1, t2))
    assert result.avg_seconds == pytest.approx((t1 + t2) / 2)


# ---------------------------------------------------------------------------
# 8. Empty merchant
# ---------------------------------------------------------------------------

def test_empty_merchant_returns_stable_zero_and_empty_metrics(db_conn, demo_merchant_id):
    assert decision_type_distribution(db_conn, demo_merchant_id).counts == {}

    policy = policy_outcome_distribution(db_conn, demo_merchant_id)
    assert (policy.allow, policy.approval_required, policy.block) == (0, 0, 0)

    capture = capture_terminal_status_distribution(db_conn, demo_merchant_id)
    assert (capture.verified_success, capture.verified_failed, capture.escalated, capture.blocked) == (0, 0, 0, 0)

    escalation = escalation_metrics(db_conn, demo_merchant_id)
    assert escalation.total_escalated == 0
    assert escalation.by_reason == {}

    attempts = verification_read_attempt_distribution(db_conn, demo_merchant_id)
    assert attempts.by_attempt_count == {}

    amount = verified_captured_amount(db_conn, demo_merchant_id)
    assert (amount.verified_success_count, amount.total_verified_captured_amount) == (0, 0)

    timing = verification_resolution_timing(db_conn, demo_merchant_id)
    assert timing.count == 0
    assert timing.min_seconds is None
    assert timing.max_seconds is None
    assert timing.avg_seconds is None

    retry = retry_prompt_outcome_availability(db_conn, demo_merchant_id)
    assert isinstance(retry, RetryPromptOutcomeAvailability)
    assert retry.total_customer_retry_prompt_actions == 0
    assert retry.outcome_measurable is False


# ---------------------------------------------------------------------------
# 9. Merchant isolation (cross-metric)
# ---------------------------------------------------------------------------

def test_merchant_isolation_across_metrics(db_conn, demo_merchant_id):
    other_merchant_id = _second_merchant(db_conn)

    _verified_success_capture(db_conn, demo_merchant_id, "order_iso_a", amount=10000)
    _verified_success_capture(db_conn, other_merchant_id, "order_iso_b", amount=10000)

    result_a = verified_captured_amount(db_conn, demo_merchant_id)
    result_b = verified_captured_amount(db_conn, other_merchant_id)

    assert result_a.verified_success_count == 1
    assert result_b.verified_success_count == 1
    assert result_a.total_verified_captured_amount == 10000
    assert result_b.total_verified_captured_amount == 10000


# ---------------------------------------------------------------------------
# 10. Read-only behavior
# ---------------------------------------------------------------------------

def test_observability_functions_are_read_only(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_ro_setup")

    def _row_counts() -> dict[str, int]:
        with db_conn.cursor() as cur:
            cur.execute("select count(*) from decisions")
            decisions = cur.fetchone()[0]
            cur.execute("select count(*) from actions")
            actions = cur.fetchone()[0]
            cur.execute("select count(*) from audit_entries")
            audit_entries = cur.fetchone()[0]
        return {"decisions": decisions, "actions": actions, "audit_entries": audit_entries}

    before = _row_counts()

    decision_type_distribution(db_conn, demo_merchant_id)
    policy_outcome_distribution(db_conn, demo_merchant_id)
    capture_terminal_status_distribution(db_conn, demo_merchant_id)
    escalation_metrics(db_conn, demo_merchant_id)
    verification_read_attempt_distribution(db_conn, demo_merchant_id)
    verified_captured_amount(db_conn, demo_merchant_id)
    verification_resolution_timing(db_conn, demo_merchant_id)
    retry_prompt_outcome_availability(db_conn, demo_merchant_id)

    after = _row_counts()
    assert after == before


# ---------------------------------------------------------------------------
# 11. Determinism
# ---------------------------------------------------------------------------

def test_determinism_identical_state_produces_identical_output(db_conn, demo_merchant_id):
    _capture_decision(db_conn, demo_merchant_id, "order_det_1", 10000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_det_2", amount=20000)
    _escalated_unexpected_status_capture(db_conn, demo_merchant_id, "order_det_3")

    assert (
        decision_type_distribution(db_conn, demo_merchant_id).model_dump()
        == decision_type_distribution(db_conn, demo_merchant_id).model_dump()
    )
    assert (
        capture_terminal_status_distribution(db_conn, demo_merchant_id).model_dump_json()
        == capture_terminal_status_distribution(db_conn, demo_merchant_id).model_dump_json()
    )
    assert (
        escalation_metrics(db_conn, demo_merchant_id).model_dump_json()
        == escalation_metrics(db_conn, demo_merchant_id).model_dump_json()
    )


# ---------------------------------------------------------------------------
# 12. Semantic separation: BLOCKED is never a verification outcome
# ---------------------------------------------------------------------------

def test_blocked_capture_is_never_counted_as_a_verification_outcome(db_conn, demo_merchant_id):
    _blocked_capture(db_conn, demo_merchant_id, "order_sep_blocked")

    policy = policy_outcome_distribution(db_conn, demo_merchant_id)
    assert policy.block == 1
    assert policy.allow == 0
    assert policy.approval_required == 0

    capture = capture_terminal_status_distribution(db_conn, demo_merchant_id)
    assert capture.blocked == 1
    assert capture.verified_failed == 0
    assert capture.escalated == 0
    assert capture.verified_success == 0

    escalation = escalation_metrics(db_conn, demo_merchant_id)
    assert escalation.total_escalated == 0  # a block is not an escalation


# ---------------------------------------------------------------------------
# CUSTOMER_RETRY_PROMPT outcome unavailability
# ---------------------------------------------------------------------------

def test_retry_prompt_outcome_availability_reports_unmeasurable_not_zero(db_conn, demo_merchant_id):
    decision_id = _retry_prompt_decision(db_conn, demo_merchant_id, "order_rp_avail")
    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 20000, "approval_band_upper": 100000})
    action = propose_action(db_conn, decision_id, write_client=SpyWriteClient())
    assert action["action_type"] == "CUSTOMER_RETRY_PROMPT"

    result = retry_prompt_outcome_availability(db_conn, demo_merchant_id)

    assert isinstance(result, RetryPromptOutcomeAvailability)
    assert result.total_customer_retry_prompt_actions == 1
    assert result.outcome_measurable is False
