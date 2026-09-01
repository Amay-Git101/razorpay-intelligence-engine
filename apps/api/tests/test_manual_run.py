"""Tests for the manual pipeline runner's own thin orchestration/
presentation contract -- argument parsing, which functions it calls and
in what order, and its output/exit-status behavior.

Deliberately does NOT re-test reconciliation/RuleBasedEngine/Policy/
Action/capture/Verification/feedback calibration themselves -- those
are already fully covered elsewhere. Every pipeline function manual_run
calls is replaced with a monkeypatched fake here; these tests exist to
prove manual_run sequences and reports on them correctly, nothing more.

Pure Python -- no DATABASE_URL, no network, no live Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import manual_run.run_reconciliation as runner
from policy.orchestration import NotPolicyGated
from razorpay_client.errors import RazorpayAPIError


class _FakeConn:
    """Stands in for a psycopg.Connection -- only commit()/rollback()
    are ever called on it by manual_run itself (every real write goes
    through the monkeypatched pipeline functions below, never through
    this object directly)."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeReadClient:
    """Stands in for RazorpayReadClient -- never actually used by these
    tests since reconcile_order()/verify_action() are themselves
    monkeypatched, but passed through exactly as main() would."""


def _install_common_fakes(monkeypatch, *, merchant: dict[str, Any] | None = "DEFAULT"):
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


def test_merchant_not_found_prevents_reconciliation(monkeypatch, capsys):
    _install_common_fakes(monkeypatch, merchant=None)
    called = {"reconcile": False}
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: called.__setitem__("reconcile", True) or [])

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "missing_merchant", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    assert called["reconcile"] is False
    out = capsys.readouterr().out
    assert "not found" in out


# ---------------------------------------------------------------------------
# Zero new events
# ---------------------------------------------------------------------------


def test_zero_new_events_produces_a_clean_successful_result(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [])
    make_decision_calls = []
    monkeypatch.setattr(runner, "make_decision", lambda *a, **k: make_decision_calls.append(a) or uuid.uuid4())

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert make_decision_calls == []
    out = capsys.readouterr().out
    assert "0 new event" in out
    assert "nothing new" in out


# ---------------------------------------------------------------------------
# NO_ACTION vs. action-eligible decisions
# ---------------------------------------------------------------------------


def _one_new_event_fixture(monkeypatch, event_type: str = "payment.attempt.authorized"):
    event_id = uuid.uuid4()
    event = {"id": event_id, "event_type": event_type, "order_id": "order_x"}
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [event_id])
    monkeypatch.setattr(runner, "list_events_for_order", lambda conn, order_id: [event])
    return event_id


def test_no_action_decision_does_not_call_propose_action(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    decision_id = uuid.uuid4()
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: decision_id)
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "NO_ACTION"})
    propose_calls = []
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: propose_calls.append(a) or {})

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert propose_calls == []
    out = capsys.readouterr().out
    assert "decision: NO_ACTION" in out
    assert "not proposed" in out


def test_non_no_action_decision_calls_propose_action(monkeypatch):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    decision_id = uuid.uuid4()
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: decision_id)
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    propose_calls = []
    monkeypatch.setattr(
        runner, "propose_action",
        lambda conn, did, write_client=None: propose_calls.append((did, write_client)) or {"id": uuid.uuid4(), "status": "BLOCKED"},
    )

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert len(propose_calls) == 1
    assert propose_calls[0] == (decision_id, None)  # write_client=None -- orchestrator owns construction


def test_propose_action_is_never_called_with_a_constructed_write_client(monkeypatch):
    # Structural guard, independent of the architecture-boundary source
    # scan: even if manual_run somehow had a write client available, it
    # must always pass write_client=None.
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_RETRY_PROMPT"})

    def _propose(conn, decision_id, write_client=None):
        assert write_client is None
        return {"id": uuid.uuid4(), "status": "AUTHORIZED"}

    monkeypatch.setattr(runner, "propose_action", _propose)

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)


# ---------------------------------------------------------------------------
# Verification only for VERIFYING actions
# ---------------------------------------------------------------------------


def test_verifying_action_calls_verify_action(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    action_id = uuid.uuid4()
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: {"id": action_id, "status": "VERIFYING"})
    verify_calls = []
    monkeypatch.setattr(
        runner, "verify_action",
        lambda conn, aid, read_client=None: verify_calls.append(aid) or {"status": "VERIFIED_SUCCESS"},
    )

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert verify_calls == [action_id]
    out = capsys.readouterr().out
    assert "verification: VERIFIED_SUCCESS" in out


@pytest.mark.parametrize("non_verifying_status", ["BLOCKED", "APPROVAL_PENDING", "AUTHORIZED"])
def test_non_verifying_action_does_not_call_verify_action(monkeypatch, capsys, non_verifying_status):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: {"id": uuid.uuid4(), "status": non_verifying_status})
    verify_calls = []
    monkeypatch.setattr(runner, "verify_action", lambda *a, **k: verify_calls.append(a) or {})

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert verify_calls == []
    out = capsys.readouterr().out
    assert f"action: {non_verifying_status}" in out
    assert "skipped" in out


def test_approval_pending_is_reported_and_never_auto_approved(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: {"id": uuid.uuid4(), "status": "APPROVAL_PENDING"})

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    out = capsys.readouterr().out
    assert "action: APPROVAL_PENDING" in out
    # manual_run must never even be able to auto-approve -- it doesn't
    # import grant_approval/reject_approval at all.
    assert not hasattr(runner, "grant_approval")
    assert not hasattr(runner, "reject_approval")


def test_unsupported_decision_type_is_reported_not_crashed(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_MERCHANT_ACTION"})

    def _raise_not_gated(*a, **k):
        raise NotPolicyGated("unsupported decision_type")

    monkeypatch.setattr(runner, "propose_action", _raise_not_gated)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "not proposed" in out


# ---------------------------------------------------------------------------
# Feedback calibration
# ---------------------------------------------------------------------------


def test_recalibrate_flag_invokes_feedback_calibration(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(
        runner, "recompute_baselines",
        lambda conn, merchant_id: calls.append(merchant_id) or _FakeReport(buckets_processed=2),
    )

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=True)

    assert exit_code == runner.EXIT_OK
    assert calls == ["m1"]
    out = capsys.readouterr().out
    assert "calibration: completed" in out


def test_without_recalibrate_flag_calibration_is_not_invoked(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(runner, "recompute_baselines", lambda *a, **k: calls.append(True) or _FakeReport())

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert calls == []
    out = capsys.readouterr().out
    assert "calibration" not in out


class _FakeReport:
    def __init__(self, buckets_processed: int = 0):
        self.buckets_processed = buckets_processed


def test_calibration_failure_is_reported_separately_from_an_established_payment_result(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    action_id = uuid.uuid4()
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: {"id": action_id, "status": "VERIFYING"})
    monkeypatch.setattr(runner, "verify_action", lambda *a, **k: {"status": "VERIFIED_SUCCESS"})

    def _broken_calibration(*a, **k):
        raise RuntimeError("simulated calibration failure")

    monkeypatch.setattr(runner, "recompute_baselines", _broken_calibration)
    conn = _FakeConn()

    exit_code = runner.run(conn, _FakeReadClient(), "m1", "order_x", recalibrate=True)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    out = capsys.readouterr().out
    assert "verification: VERIFIED_SUCCESS" in out  # the payment outcome is still reported, unaltered
    assert "calibration: failed" in out
    assert "unaffected" in out
    assert conn.rollbacks == 1  # only calibration's own attempt is rolled back


# ---------------------------------------------------------------------------
# Razorpay read failure
# ---------------------------------------------------------------------------


def test_razorpay_read_failure_is_reported_cleanly(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)

    def _raise_api_error(*a, **k):
        raise RazorpayAPIError("Razorpay API returned HTTP 404 for /orders/order_x")

    monkeypatch.setattr(runner, "reconcile_order", _raise_api_error)

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    out = capsys.readouterr().out
    assert "Razorpay read failed" in out


# ---------------------------------------------------------------------------
# Only reconciliation-returned event ids are processed
# ---------------------------------------------------------------------------


def test_only_reconciliation_returned_event_ids_are_processed(monkeypatch):
    _install_common_fakes(monkeypatch)
    new_id = uuid.uuid4()
    old_id = uuid.uuid4()
    new_event = {"id": new_id, "event_type": "payment.attempt.authorized", "order_id": "order_x"}
    old_event = {"id": old_id, "event_type": "payment.attempt.failed", "order_id": "order_x"}
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [new_id])
    monkeypatch.setattr(runner, "list_events_for_order", lambda conn, order_id: [old_event, new_event])

    processed_event_ids = []
    monkeypatch.setattr(
        runner, "make_decision",
        lambda conn, merchant_id, event: processed_event_ids.append(event["id"]) or uuid.uuid4(),
    )
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "NO_ACTION"})

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OK
    assert processed_event_ids == [new_id]  # old_event, present in list_events_for_order, was never touched


def test_unresolved_returned_event_id_fails_rather_than_silently_processing_the_wrong_event(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    unresolvable_id = uuid.uuid4()
    unrelated_event = {"id": uuid.uuid4(), "event_type": "payment.attempt.failed", "order_id": "order_x"}
    monkeypatch.setattr(runner, "reconcile_order", lambda *a, **k: [unresolvable_id])
    monkeypatch.setattr(runner, "list_events_for_order", lambda conn, order_id: [unrelated_event])

    make_decision_calls = []
    monkeypatch.setattr(runner, "make_decision", lambda *a, **k: make_decision_calls.append(a) or uuid.uuid4())

    exit_code = runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=False)

    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
    assert make_decision_calls == []  # never fell back to processing the unrelated event instead
    out = capsys.readouterr().out
    assert "could not be resolved" in out


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------


def test_output_never_contains_credential_like_values(monkeypatch, capsys):
    _install_common_fakes(monkeypatch)
    _one_new_event_fixture(monkeypatch)
    monkeypatch.setattr(runner, "make_decision", lambda conn, merchant_id, event: uuid.uuid4())
    monkeypatch.setattr(runner, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(runner, "propose_action", lambda *a, **k: {"id": uuid.uuid4(), "status": "VERIFYING"})
    monkeypatch.setattr(runner, "verify_action", lambda *a, **k: {"status": "VERIFIED_SUCCESS"})
    monkeypatch.setattr(runner, "recompute_baselines", lambda *a, **k: _FakeReport(buckets_processed=1))

    fake_secret = "sk_test_definitely_not_a_real_secret_value_12345"
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", fake_secret)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:definitely_not_real@host/db")

    runner.run(_FakeConn(), _FakeReadClient(), "m1", "order_x", recalibrate=True)

    out = capsys.readouterr().out
    assert fake_secret not in out
    assert "definitely_not_real" not in out


def test_missing_credentials_message_names_the_variables_not_their_values(capsys):
    exit_code = 1
    try:
        from razorpay_client.client import RazorpayReadClient

        RazorpayReadClient(key_id=None, key_secret=None)
    except RuntimeError as exc:
        message = str(exc)
        exit_code = runner.EXIT_OPERATIONAL_ERROR
        assert "RAZORPAY_KEY_ID" in message
        assert "RAZORPAY_KEY_SECRET" in message
    assert exit_code == runner.EXIT_OPERATIONAL_ERROR
