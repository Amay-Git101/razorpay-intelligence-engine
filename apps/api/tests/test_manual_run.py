"""Tests for the manual pipeline runner's own thin CLI contract --
argument parsing, merchant validation, delegation to
pipeline.orchestration.run_reconciliation_pipeline(), and presentation/
exit-status behavior.

Deliberately does NOT re-test the reconcile -> decide -> policy ->
action -> verify sequencing itself -- that now lives in
pipeline/orchestration.py and is tested in test_pipeline_orchestration.py.
This file only proves manual_run calls that shared function correctly
and reports on its result (and on feedback calibration) correctly.

Pure Python -- no DATABASE_URL, no network, no live Postgres.
"""

from __future__ import annotations

import uuid

import pytest

import manual_run.run_reconciliation as runner
from pipeline.orchestration import EventProcessingResult, PipelineRunResult, UnresolvedEventError
from razorpay_client.errors import RazorpayAPIError


class _FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeReadClient:
    """Stands in for RazorpayReadClient -- never actually used by these
    tests since run_reconciliation_pipeline() is itself monkeypatched."""


class _FakeReport:
    def __init__(self, buckets_processed: int = 0):
        self.buckets_processed = buckets_processed


def _install_merchant(monkeypatch, merchant: dict | None = "DEFAULT"):
    if merchant == "DEFAULT":
        merchant = {"id": "m1", "name": "Demo"}
    monkeypatch.setattr(runner, "get_merchant", lambda conn, merchant_id: merchant)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_argument_parsing_accepts_merchant_id_and_order_id():
    args = runner._parse_args(["--merchant-id", "m1", "--order-id", "order_x"])
    assert args.merchant_id == "m1"
    assert args.order_id == "order_x"
    assert args.recalibrate is False


def test_argument_parsing_accepts_recalibrate_flag():
    args = runner._parse_args(["--merchant-id", "m1", "--order-id", "order_x", "--recalibrate"])
    assert args.recalibrate is True


def test_missing_required_arguments_fail_cleanly():
    with pytest.raises(SystemExit) as excinfo:
        runner._parse_args([])
    assert excinfo.value.code != 0


def test_missing_order_id_fails_cleanly():
    with pytest.raises(SystemExit):
        runner._parse_args(["--merchant-id", "m1"])


# ---------------------------------------------------------------------------
# Merchant validation
# ---------------------------------------------------------------------------


def test_merchant_not_found_prevents_pipeline_invocation(monkeypatch, capsys):
    _install_merchant(monkeypatch, merchant=None)
    called = []
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: called.append(True))

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "missing_merchant", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    assert called == []
    assert "not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Delegation to the shared pipeline function
# ---------------------------------------------------------------------------


def test_run_delegates_to_the_shared_pipeline_function(monkeypatch):
    _install_merchant(monkeypatch)
    calls = []

    def _fake_pipeline(conn, read_client, merchant_id, order_id):
        calls.append((merchant_id, order_id))
        return PipelineRunResult(order_id=order_id, new_event_count=0, events=[])

    monkeypatch.setattr(runner, "run_reconciliation_pipeline", _fake_pipeline)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert calls == [("m1", "order_x")]


def test_zero_new_events_produces_a_clean_successful_result(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    monkeypatch.setattr(
        runner, "run_reconciliation_pipeline",
        lambda *a, **k: PipelineRunResult(order_id="order_x", new_event_count=0, events=[]),
    )

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "0 new event" in out
    assert "nothing new" in out


def test_no_action_event_is_reported_as_not_proposed(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.failed",
            decision_id="d1", decision_type="NO_ACTION", action_skipped_reason="NO_ACTION",
        )],
    )
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: result)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "decision: NO_ACTION" in out
    assert "not proposed" in out


def test_verified_action_result_is_reported(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.authorized",
            decision_id="d1", decision_type="RECOMMEND_CAPTURE",
            action_id="a1", action_status="VERIFYING", verification_status="VERIFIED_SUCCESS",
        )],
    )
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: result)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "action: VERIFYING" in out
    assert "verification: VERIFIED_SUCCESS" in out


def test_approval_pending_is_reported_and_never_auto_approved(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.authorized",
            decision_id="d1", decision_type="RECOMMEND_CAPTURE",
            action_id="a1", action_status="APPROVAL_PENDING",
        )],
    )
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: result)

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    out = capsys.readouterr().out
    assert "action: APPROVAL_PENDING" in out
    assert not hasattr(runner, "grant_approval")
    assert not hasattr(runner, "reject_approval")


# ---------------------------------------------------------------------------
# Feedback calibration
# ---------------------------------------------------------------------------


def test_recalibrate_flag_invokes_feedback_calibration(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    monkeypatch.setattr(
        runner, "run_reconciliation_pipeline",
        lambda *a, **k: PipelineRunResult(order_id="order_x", new_event_count=0, events=[]),
    )
    calls = []
    monkeypatch.setattr(runner, "recompute_baselines", lambda conn, merchant_id: calls.append(merchant_id) or _FakeReport(2))

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=True)

    assert exit_code == runner.EXIT_OK
    assert calls == ["m1"]
    assert "calibration: completed" in capsys.readouterr().out


def test_without_recalibrate_flag_calibration_is_not_invoked(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    monkeypatch.setattr(
        runner, "run_reconciliation_pipeline",
        lambda *a, **k: PipelineRunResult(order_id="order_x", new_event_count=0, events=[]),
    )
    calls = []
    monkeypatch.setattr(runner, "recompute_baselines", lambda *a, **k: calls.append(True) or _FakeReport())

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert calls == []
    assert "calibration" not in capsys.readouterr().out


def test_calibration_failure_is_reported_separately_from_an_established_payment_result(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.authorized",
            decision_id="d1", decision_type="RECOMMEND_CAPTURE",
            action_id="a1", action_status="VERIFYING", verification_status="VERIFIED_SUCCESS",
        )],
    )
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: result)

    def _broken_calibration(*a, **k):
        raise RuntimeError("simulated calibration failure")

    monkeypatch.setattr(runner, "recompute_baselines", _broken_calibration)
    conn = _FakeConn()

    exit_code = runner.run(conn, _FakeReadClient(), "m1", "order_x", recalibrate=True)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    out = capsys.readouterr().out
    assert "verification: VERIFIED_SUCCESS" in out  # payment outcome still reported, unaltered
    assert "calibration: failed" in out
    assert "unaffected" in out
    assert conn.rollbacks == 1


# ---------------------------------------------------------------------------
# Pipeline-level failures
# ---------------------------------------------------------------------------


def test_razorpay_read_failure_is_reported_cleanly(monkeypatch, capsys):
    _install_merchant(monkeypatch)

    def _raise_api_error(*a, **k):
        raise RazorpayAPIError("Razorpay API returned HTTP 404 for /orders/order_x")

    monkeypatch.setattr(runner, "run_reconciliation_pipeline", _raise_api_error)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    assert "Razorpay read failed" in capsys.readouterr().out


def test_unresolved_event_error_is_reported_cleanly(monkeypatch, capsys):
    _install_merchant(monkeypatch)

    def _raise_unresolved(*a, **k):
        raise UnresolvedEventError("could not resolve event")

    monkeypatch.setattr(runner, "run_reconciliation_pipeline", _raise_unresolved)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    assert "could not be resolved" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------


def test_output_never_contains_credential_like_values(monkeypatch, capsys):
    _install_merchant(monkeypatch)
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.authorized",
            decision_id="d1", decision_type="RECOMMEND_CAPTURE",
            action_id="a1", action_status="VERIFYING", verification_status="VERIFIED_SUCCESS",
        )],
    )
    monkeypatch.setattr(runner, "run_reconciliation_pipeline", lambda *a, **k: result)
    monkeypatch.setattr(runner, "recompute_baselines", lambda *a, **k: _FakeReport(1))

    fake_secret = "sk_test_definitely_not_a_real_secret_value_12345"
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", fake_secret)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:definitely_not_real@host/db")

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=True)

    out = capsys.readouterr().out
    assert fake_secret not in out
    assert "definitely_not_real" not in out


def test_missing_credentials_message_names_the_variables_not_their_values():
    from razorpay_client.client import RazorpayReadClient

    with pytest.raises(RuntimeError) as excinfo:
        RazorpayReadClient(key_id=None, key_secret=None)
    message = str(excinfo.value)
    assert "RAZORPAY_KEY_ID" in message
    assert "RAZORPAY_KEY_SECRET" in message
