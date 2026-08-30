"""Feedback calibration tests. Pure-Python aggregation-logic tests plus
DB-integration tests requiring live Postgres.

Fixture builders here mirror the same real-pipeline pattern already used
by test_observability_metrics.py and test_verification.py (reconcile_order
-> propose_action -> verify_action) rather than importing from those
test_*.py files directly -- shared, non-test helpers come only from
support.py, per this project's existing convention.
"""

from __future__ import annotations

from typing import Any, Callable

import psycopg
import pytest

from action.orchestrator import propose_action
from intelligence.orchestration import make_decision
from feedback.calibration import (
    CAPTURE_PAYMENT_ACTION_TYPE,
    TERMINAL_STATUSES,
    BucketRecoveryObservation,
    FeedbackReport,
    recompute_baselines,
)
from razorpay_client.errors import RazorpayAPIError
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.decisions import insert_decision
from repository.expectation_baselines import get_baseline
from repository.merchants import insert_merchant
from support import FakeReconciliationClient, SpyReadClient, SpyWriteClient, set_policy_config
from verification.verifier import verify_action

# ---------------------------------------------------------------------------
# Fixture builders -- real Action/Verification rows via the actual
# production pipeline, never hand-crafted actions rows.
# ---------------------------------------------------------------------------


def _insert_capture_decision_with_bucket(
    conn: psycopg.Connection, merchant_id: str, order_id: str, payment_attempt_id: str, amount: int, bucket_key: str,
):
    """Same shape as support.insert_capture_decision(), but with a
    caller-controlled bucket_key -- needed to test bucket isolation,
    which the fixed 'n/a' bucket_key in support.py's helper can't do."""
    return insert_decision(
        conn, merchant_id, order_id, payment_attempt_id,
        str(list_events_for_order(conn, order_id)[0]["id"]),
        {"fields": []},
        {"bucket_key": bucket_key, "expected_recovery_rate": 0.5, "sample_size": 0, "source": "test_fixture"},
        "RECOMMEND_CAPTURE", 1.0, ["TEST_FIXTURE_CAPTURE_RECOMMENDATION"],
        {"revenue_at_stake": amount}, "test_fixture",
    )


def _reconciled_order(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int) -> str:
    payment_id = f"pay_{order_id}"
    client = FakeReconciliationClient(order_id, payment_id, amount)
    reconcile_order(conn, client, merchant_id, order_id)
    return payment_id


def _verifying_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str,
) -> dict[str, Any]:
    payment_id = _reconciled_order(conn, merchant_id, order_id, amount)
    decision_id = _insert_capture_decision_with_bucket(conn, merchant_id, order_id, payment_id, amount, bucket_key)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["status"] == "VERIFYING"
    return action


def _verified_success_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str = "no_error_reason",
) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount, bucket_key)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "VERIFIED_SUCCESS"
    return final


def _verified_failed_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str = "no_error_reason",
) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount, bucket_key)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "authorized", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "VERIFIED_FAILED"
    return final


def _escalated_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str = "no_error_reason",
) -> dict[str, Any]:
    action = _verifying_capture(conn, merchant_id, order_id, amount, bucket_key)
    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "refunded", "amount": amount})
    final = verify_action(conn, action["id"], read_client=read_client)
    assert final["status"] == "ESCALATED"
    return final


def _blocked_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str = "no_error_reason",
) -> dict[str, Any]:
    payment_id = _reconciled_order(conn, merchant_id, order_id, amount)
    decision_id = _insert_capture_decision_with_bucket(conn, merchant_id, order_id, payment_id, amount, bucket_key)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 100, "approval_band_upper": 200})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["status"] == "BLOCKED"
    return action


def _authorized_non_terminal_capture(
    conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int, bucket_key: str = "no_error_reason",
) -> dict[str, Any]:
    """A capture that has executed but never been verified -- stays at
    VERIFYING. Proves non-terminal statuses are excluded from feedback."""
    action = _verifying_capture(conn, merchant_id, order_id, amount, bucket_key)
    assert action["status"] == "VERIFYING"
    return action


def _retry_prompt_action(conn: psycopg.Connection, merchant_id: str, order_id: str, amount: int) -> dict[str, Any]:
    client = _RetryFailureClient(order_id, amount)
    reconcile_order(conn, client, merchant_id, order_id)
    events = list_events_for_order(conn, order_id)
    event = next(e for e in events if e["event_type"] == "payment.attempt.failed")
    decision_id = make_decision(conn, merchant_id, event)
    set_policy_config(conn, merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    action = propose_action(conn, decision_id, write_client=SpyWriteClient())
    assert action["action_type"] == "CUSTOMER_RETRY_PROMPT"
    return action


class _RetryFailureClient:
    def __init__(self, order_id: str, amount: int):
        self._order_id = order_id
        self._amount = amount

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": self._order_id, "amount": self._amount, "amount_paid": 0, "amount_due": self._amount,
            "currency": "INR", "status": "created", "attempts": 1,
        }

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return [{
            "id": f"pay_{self._order_id}", "order_id": self._order_id, "status": "failed", "amount": self._amount,
            "method": "card", "captured": False,
            "error_source": "gateway", "error_step": "payment_authorization", "error_reason": "payment_failed",
        }]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _second_merchant(conn: psycopg.Connection) -> str:
    return str(insert_merchant(conn, "Second Merchant", {}, {}))


# ---------------------------------------------------------------------------
# Pure-Python: bucket/aggregation logic
# ---------------------------------------------------------------------------


def test_module_exports_expected_constants():
    assert CAPTURE_PAYMENT_ACTION_TYPE == "CAPTURE_PAYMENT"
    assert TERMINAL_STATUSES == ("VERIFIED_SUCCESS", "VERIFIED_FAILED")


def test_bucket_observation_recovery_rate_is_success_over_total():
    obs = BucketRecoveryObservation(
        merchant_id="m1", bucket_key="no_error_reason",
        verified_success_count=3, verified_failed_count=1,
        total_terminal_observations=4, recovery_rate=0.75, baseline_written=True,
    )
    assert obs.recovery_rate == pytest.approx(obs.verified_success_count / obs.total_terminal_observations)


def test_feedback_report_scope_note_names_its_limitations():
    report = FeedbackReport(
        merchants_processed=[], buckets_processed=0, total_terminal_observations=0,
        total_verified_success=0, total_verified_failed=0, bucket_results=[],
    )
    assert "CUSTOMER_RETRY_PROMPT" in report.scope_note
    assert "1.0" in report.scope_note
    assert "accuracy" not in report.scope_note.lower()


def test_report_serialization_is_deterministic_for_identical_data():
    kwargs = dict(
        merchants_processed=["m1"], buckets_processed=1, total_terminal_observations=2,
        total_verified_success=1, total_verified_failed=1,
        bucket_results=[
            BucketRecoveryObservation(
                merchant_id="m1", bucket_key="no_error_reason",
                verified_success_count=1, verified_failed_count=1,
                total_terminal_observations=2, recovery_rate=0.5, baseline_written=True,
            )
        ],
    )
    first = FeedbackReport(**kwargs).model_dump_json()
    second = FeedbackReport(**kwargs).model_dump_json()
    assert first == second


# ---------------------------------------------------------------------------
# DB integration: evidence unit + exclusions
# ---------------------------------------------------------------------------


def test_one_verified_success_produces_one_positive_observation(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_success", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.buckets_processed == 1
    bucket = report.bucket_results[0]
    assert bucket.verified_success_count == 1
    assert bucket.verified_failed_count == 0
    assert bucket.total_terminal_observations == 1
    assert bucket.recovery_rate == 1.0
    assert bucket.baseline_written is True


def test_one_verified_failed_produces_one_negative_observation(db_conn, demo_merchant_id):
    _verified_failed_capture(db_conn, demo_merchant_id, "order_fb_failed", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    bucket = report.bucket_results[0]
    assert bucket.verified_success_count == 0
    assert bucket.verified_failed_count == 1
    assert bucket.total_terminal_observations == 1
    assert bucket.recovery_rate == 0.0


def test_mixed_success_and_failure_produces_correct_aggregate_rate(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_mix_1", amount=10000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_mix_2", amount=10000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_mix_3", amount=10000)
    _verified_failed_capture(db_conn, demo_merchant_id, "order_fb_mix_4", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    bucket = report.bucket_results[0]
    assert bucket.verified_success_count == 3
    assert bucket.verified_failed_count == 1
    assert bucket.total_terminal_observations == 4
    assert bucket.recovery_rate == pytest.approx(0.75)


def test_escalated_is_excluded(db_conn, demo_merchant_id):
    _escalated_capture(db_conn, demo_merchant_id, "order_fb_escalated", amount=10000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_escalated_control", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.total_terminal_observations == 1
    assert report.bucket_results[0].total_terminal_observations == 1


def test_blocked_is_excluded(db_conn, demo_merchant_id):
    _blocked_capture(db_conn, demo_merchant_id, "order_fb_blocked", amount=500000)
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_blocked_control", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.total_terminal_observations == 1


def test_non_terminal_actions_are_excluded(db_conn, demo_merchant_id):
    _authorized_non_terminal_capture(db_conn, demo_merchant_id, "order_fb_nonterminal", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.buckets_processed == 0
    assert report.total_terminal_observations == 0


def test_customer_retry_prompt_is_excluded(db_conn, demo_merchant_id):
    action = _retry_prompt_action(db_conn, demo_merchant_id, "order_fb_retry_prompt", amount=20000)
    assert action["status"] == "AUTHORIZED"  # never reaches Verification -- confirms the premise

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.buckets_processed == 0
    assert report.total_terminal_observations == 0


# ---------------------------------------------------------------------------
# DB integration: merchant/bucket isolation
# ---------------------------------------------------------------------------


def test_two_merchants_with_identical_bucket_keys_remain_isolated(db_conn, demo_merchant_id):
    other_merchant_id = _second_merchant(db_conn)

    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_iso_a", amount=10000, bucket_key="shared_bucket")
    _verified_failed_capture(db_conn, other_merchant_id, "order_fb_iso_b", amount=10000, bucket_key="shared_bucket")

    report_a = recompute_baselines(db_conn, demo_merchant_id)
    report_b = recompute_baselines(db_conn, other_merchant_id)

    assert report_a.bucket_results[0].recovery_rate == 1.0
    assert report_b.bucket_results[0].recovery_rate == 0.0

    baseline_a = get_baseline(db_conn, demo_merchant_id, "shared_bucket")
    baseline_b = get_baseline(db_conn, other_merchant_id, "shared_bucket")
    assert float(baseline_a["recovery_rate"]) == 1.0
    assert float(baseline_b["recovery_rate"]) == 0.0


def test_two_buckets_for_the_same_merchant_remain_isolated(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_bucket_x", amount=10000, bucket_key="bucket_x")
    _verified_failed_capture(db_conn, demo_merchant_id, "order_fb_bucket_y", amount=10000, bucket_key="bucket_y")

    report = recompute_baselines(db_conn, demo_merchant_id)

    by_bucket = {b.bucket_key: b for b in report.bucket_results}
    assert by_bucket["bucket_x"].recovery_rate == 1.0
    assert by_bucket["bucket_y"].recovery_rate == 0.0


def test_recompute_with_no_merchant_filter_covers_all_merchants_without_merging(db_conn, demo_merchant_id):
    other_merchant_id = _second_merchant(db_conn)
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_all_a", amount=10000)
    _verified_failed_capture(db_conn, other_merchant_id, "order_fb_all_b", amount=10000)

    report = recompute_baselines(db_conn)  # no merchant_id -- covers everything visible in this transaction

    assert demo_merchant_id in report.merchants_processed
    assert other_merchant_id in report.merchants_processed
    rates_by_merchant = {b.merchant_id: b.recovery_rate for b in report.bucket_results}
    assert rates_by_merchant[demo_merchant_id] == 1.0
    assert rates_by_merchant[other_merchant_id] == 0.0


# ---------------------------------------------------------------------------
# DB integration: idempotency, backfill, duplicate decisions
# ---------------------------------------------------------------------------


def test_repeated_feedback_runs_do_not_double_count(db_conn, demo_merchant_id):
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_rerun", amount=10000)

    first = recompute_baselines(db_conn, demo_merchant_id)
    second = recompute_baselines(db_conn, demo_merchant_id)
    third = recompute_baselines(db_conn, demo_merchant_id)

    assert first.model_dump(exclude={"bucket_results"}) == second.model_dump(exclude={"bucket_results"}) == third.model_dump(exclude={"bucket_results"})
    assert third.bucket_results[0].total_terminal_observations == 1


def test_historical_preexisting_terminal_actions_are_included(db_conn, demo_merchant_id):
    # Simulates "backfill": the action existed before recompute_baselines
    # was ever called -- there is no separate backfill path, this IS it.
    _verified_success_capture(db_conn, demo_merchant_id, "order_fb_historical", amount=10000)
    _verified_failed_capture(db_conn, demo_merchant_id, "order_fb_historical_2", amount=10000)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.total_terminal_observations == 2


def test_duplicate_decision_for_same_payment_attempt_counts_the_action_only_once(db_conn, demo_merchant_id):
    payment_id = _reconciled_order(db_conn, demo_merchant_id, "order_fb_dup_decision", 10000)
    first_decision_id = _insert_capture_decision_with_bucket(
        db_conn, demo_merchant_id, "order_fb_dup_decision", payment_id, 10000, "no_error_reason"
    )
    second_decision_id = _insert_capture_decision_with_bucket(
        db_conn, demo_merchant_id, "order_fb_dup_decision", payment_id, 10000, "no_error_reason"
    )
    assert first_decision_id != second_decision_id  # two genuinely distinct Decision rows

    set_policy_config(db_conn, demo_merchant_id, {"max_auto_capture_amount": 1000000, "approval_band_upper": 2000000})
    write_spy = SpyWriteClient()
    action_from_first = propose_action(db_conn, first_decision_id, write_client=write_spy)
    action_from_second = propose_action(db_conn, second_decision_id, write_client=write_spy)
    assert action_from_first["id"] == action_from_second["id"]  # idempotency-key collision -> same action row

    read_client = SpyReadClient(lambda pid: {"id": pid, "status": "captured", "amount": 10000})
    verify_action(db_conn, action_from_first["id"], read_client=read_client)

    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.total_terminal_observations == 1  # not 2


# ---------------------------------------------------------------------------
# DB integration: zero-evidence case
# ---------------------------------------------------------------------------


def test_empty_merchant_does_not_create_a_zero_evidence_baseline(db_conn, demo_merchant_id):
    report = recompute_baselines(db_conn, demo_merchant_id)

    assert report.buckets_processed == 0
    assert report.bucket_results == []
    assert get_baseline(db_conn, demo_merchant_id, "no_error_reason") is None


def test_bucket_with_only_non_qualifying_actions_stays_absent(db_conn, demo_merchant_id):
    _blocked_capture(db_conn, demo_merchant_id, "order_fb_only_blocked", amount=500000, bucket_key="only_blocked")
    _escalated_capture(db_conn, demo_merchant_id, "order_fb_only_escalated", amount=10000, bucket_key="only_escalated")

    recompute_baselines(db_conn, demo_merchant_id)

    assert get_baseline(db_conn, demo_merchant_id, "only_blocked") is None
    assert get_baseline(db_conn, demo_merchant_id, "only_escalated") is None


# ---------------------------------------------------------------------------
# DB integration: calibration failure cannot alter the authoritative outcome
# ---------------------------------------------------------------------------


def test_calibration_failure_cannot_modify_already_committed_actions_or_decisions(db_conn, demo_merchant_id, monkeypatch):
    action = _verified_success_capture(db_conn, demo_merchant_id, "order_fb_failure_safety", amount=10000)

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from actions")
        actions_before = cur.fetchone()[0]
        cur.execute("select count(*) from decisions")
        decisions_before = cur.fetchone()[0]
        cur.execute("select status, outcome, verification_result from actions where id = %s", (action["id"],))
        action_row_before = cur.fetchone()

    def _broken_upsert(*args, **kwargs):
        raise RuntimeError("simulated calibration failure")

    monkeypatch.setattr("feedback.calibration.upsert_calibrated_baseline", _broken_upsert)

    with pytest.raises(RuntimeError, match="simulated calibration failure"):
        recompute_baselines(db_conn, demo_merchant_id)

    with db_conn.cursor() as cur:
        cur.execute("select count(*) from actions")
        actions_after = cur.fetchone()[0]
        cur.execute("select count(*) from decisions")
        decisions_after = cur.fetchone()[0]
        cur.execute("select status, outcome, verification_result from actions where id = %s", (action["id"],))
        action_row_after = cur.fetchone()

    assert actions_before == actions_after
    assert decisions_before == decisions_after
    assert action_row_before == action_row_after
    assert get_baseline(db_conn, demo_merchant_id, "no_error_reason") is None  # no partial write leaked either
