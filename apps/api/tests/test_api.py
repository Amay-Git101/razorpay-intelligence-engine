"""Tests for the HTTP API's own thin request/response contract --
routing, status codes, serialization, and delegation to existing
repository/observability/pipeline functions.

Deliberately does NOT re-test reconciliation/RuleBasedEngine/Policy/
Action/capture/Verification/feedback calibration/the pipeline
sequencing itself -- those already have their own comprehensive,
live-DB-tested suites (and, for the sequencing specifically,
test_pipeline_orchestration.py). Every repository/observability/
pipeline function the API calls is monkeypatched here.

Pure Python -- no DATABASE_URL, no network, no live Postgres.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

import api.app as api_app
from api.app import app, get_db
from observability.metrics import (
    CaptureTerminalStatusDistribution,
    DecisionTypeDistribution,
    EscalationMetrics,
    PolicyOutcomeDistribution,
    RetryPromptOutcomeAvailability,
    VerificationReadAttemptDistribution,
    VerificationResolutionTiming,
    VerifiedCapturedAmount,
)
from pipeline.orchestration import EventProcessingResult, PipelineRunResult, UnresolvedEventError
from razorpay_client.errors import RazorpayAPIError


class _FakeConn:
    """A DB dependency override never has to look like a real
    connection -- every function that would use it is monkeypatched."""


def _override_get_db():
    yield _FakeConn()


client = TestClient(app)
app.dependency_overrides[get_db] = _override_get_db

_VALID_MERCHANT_ID = str(uuid.uuid4())


def _merchant_row(merchant_id: str = _VALID_MERCHANT_ID) -> dict:
    return {"id": merchant_id, "name": "Demo Merchant", "created_at": __import__("datetime").datetime(2026, 1, 1)}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_succeeds():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /merchants
# ---------------------------------------------------------------------------


def test_merchant_listing_serialization(monkeypatch):
    monkeypatch.setattr(api_app, "list_merchants", lambda conn: [_merchant_row()])

    response = client.get("/merchants")

    assert response.status_code == 200
    body = response.json()
    assert body["merchants"][0]["id"] == _VALID_MERCHANT_ID
    assert body["merchants"][0]["name"] == "Demo Merchant"


# ---------------------------------------------------------------------------
# /merchants/{merchant_id}/payments
# ---------------------------------------------------------------------------


def test_merchant_payment_listing(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    order_row = {
        "id": "order_x", "merchant_id": _VALID_MERCHANT_ID, "amount": 10000, "amount_paid": 0, "amount_due": 10000,
        "status": "created", "attempts": 1, "currency": "INR", "observed_at": __import__("datetime").datetime(2026, 1, 1),
    }
    attempt_row = {
        "id": "pay_x", "order_id": "order_x", "status": "authorized", "method": "card", "captured": False,
        "amount": 10000, "error_source": None, "error_step": None, "error_reason": None,
        "observed_at": __import__("datetime").datetime(2026, 1, 1),
    }
    monkeypatch.setattr(api_app, "list_orders_for_merchant", lambda conn, mid: [order_row])
    monkeypatch.setattr(api_app, "list_payment_attempts_for_order", lambda conn, oid: [attempt_row])

    response = client.get(f"/merchants/{_VALID_MERCHANT_ID}/payments")

    assert response.status_code == 200
    body = response.json()
    assert body["merchant_id"] == _VALID_MERCHANT_ID
    assert body["orders"][0]["order"]["id"] == "order_x"
    assert body["orders"][0]["payment_attempts"][0]["id"] == "pay_x"


def test_merchant_payments_missing_merchant_returns_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: None)

    response = client.get(f"/merchants/{_VALID_MERCHANT_ID}/payments")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_malformed_merchant_id_returns_400():
    response = client.get("/merchants/not-a-uuid/payments")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /orders/{order_id} and /orders/{order_id}/timeline
# ---------------------------------------------------------------------------


def _order_row():
    import datetime
    return {
        "id": "order_x", "merchant_id": _VALID_MERCHANT_ID, "amount": 10000, "amount_paid": 10000, "amount_due": 0,
        "status": "paid", "attempts": 1, "currency": "INR", "observed_at": datetime.datetime(2026, 1, 1),
    }


def test_order_missing_returns_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: None)

    response = client.get("/orders/order_missing")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_order_timeline_missing_order_returns_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: None)

    response = client.get("/orders/order_missing/timeline")

    assert response.status_code == 404


def test_order_timeline_with_no_decisions_yet_reports_null_stages(monkeypatch):
    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: _order_row())
    monkeypatch.setattr(api_app, "list_payment_attempts_for_order", lambda conn, oid: [])
    monkeypatch.setattr(api_app, "list_decisions_for_order", lambda conn, oid: [])

    response = client.get("/orders/order_x/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] is None
    assert body["policy"] is None
    assert body["action"] is None
    assert body["verification"] is None
    assert body["outcome"] is None
    assert body["audit"] == []


def test_order_timeline_full_serialization(monkeypatch):
    import datetime
    decision_id = uuid.uuid4()
    event_id = uuid.uuid4()
    decision_row = {
        "id": decision_id, "event_id": event_id, "decision_type": "RECOMMEND_CAPTURE", "confidence": 1.0,
        "reason_codes": ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"], "expected_impact": {"revenue_at_stake": 10000},
        "model_version": "rule_v1", "created_at": datetime.datetime(2026, 1, 1),
    }
    action_row = {
        "id": uuid.uuid4(), "action_type": "CAPTURE_PAYMENT", "status": "VERIFIED_SUCCESS",
        "execution_reference": {"outcome": "success_response"},
        "policy_evaluation": {"policy_version": "policy_v1", "allowed": True, "authority_level_granted": "AUTOMATIC", "requires_approval": False, "reason_codes": ["WITHIN_AUTO_ALLOW_LIMIT"]},
        "verification_result": {"result": "VERIFIED_SUCCESS", "reason": "CAPTURED_CONFIRMED"},
        "outcome": {"recovered_amount": 10000},
    }
    audit_row = {"checkpoint": "DECISION_CREATED", "snapshot": {}, "sequence_number": 1}

    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: _order_row())
    monkeypatch.setattr(api_app, "list_payment_attempts_for_order", lambda conn, oid: [])
    monkeypatch.setattr(api_app, "list_decisions_for_order", lambda conn, oid: [decision_row])
    monkeypatch.setattr(api_app, "get_action_for_decision", lambda conn, did: action_row)
    monkeypatch.setattr(api_app, "list_audit_trail", lambda conn, eid, did: [audit_row])

    response = client.get("/orders/order_x/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["decision"]["decision_type"] == "RECOMMEND_CAPTURE"
    assert body["policy"]["allowed"] is True
    assert body["action"]["status"] == "VERIFIED_SUCCESS"
    assert body["verification"]["result"] == "VERIFIED_SUCCESS"
    assert body["outcome"]["recovered_amount"] == 10000
    assert body["audit"][0]["checkpoint"] == "DECISION_CREATED"


def test_order_timeline_blocked_action_is_not_reported_as_a_verification_outcome(monkeypatch):
    """Semantic separation: a BLOCKED action never reached Verification --
    'verification'/'outcome' must stay null, not be confused with a
    verification failure."""
    decision_row = {
        "id": uuid.uuid4(), "event_id": uuid.uuid4(), "decision_type": "RECOMMEND_CAPTURE", "confidence": 1.0,
        "reason_codes": [], "expected_impact": {}, "model_version": "rule_v1",
        "created_at": __import__("datetime").datetime(2026, 1, 1),
    }
    action_row = {
        "id": uuid.uuid4(), "action_type": "CAPTURE_PAYMENT", "status": "BLOCKED", "execution_reference": None,
        "policy_evaluation": {"allowed": False, "reason_codes": ["AMOUNT_EXCEEDS_HARD_LIMIT"]},
        "verification_result": None, "outcome": None,
    }
    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: _order_row())
    monkeypatch.setattr(api_app, "list_payment_attempts_for_order", lambda conn, oid: [])
    monkeypatch.setattr(api_app, "list_decisions_for_order", lambda conn, oid: [decision_row])
    monkeypatch.setattr(api_app, "get_action_for_decision", lambda conn, did: action_row)
    monkeypatch.setattr(api_app, "list_audit_trail", lambda conn, eid, did: [])

    response = client.get("/orders/order_x/timeline")

    body = response.json()
    assert body["action"]["status"] == "BLOCKED"
    assert body["verification"] is None
    assert body["outcome"] is None


# ---------------------------------------------------------------------------
# Reconciliation endpoint
# ---------------------------------------------------------------------------


def test_reconcile_calls_the_shared_pipeline_function_not_a_reimplementation(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "RazorpayReadClient", lambda: _FakeReadClient())
    calls = []

    def _fake_pipeline(conn, read_client, merchant_id, order_id):
        calls.append((merchant_id, order_id))
        return PipelineRunResult(order_id=order_id, new_event_count=0, events=[])

    monkeypatch.setattr(api_app, "run_reconciliation_pipeline", _fake_pipeline)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 200
    assert calls == [(_VALID_MERCHANT_ID, "order_x")]
    assert response.json()["new_event_count"] == 0


def test_reconcile_no_action_event_is_reported_without_the_api_proposing_anything(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "RazorpayReadClient", lambda: _FakeReadClient())
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.failed",
            decision_id="d1", decision_type="NO_ACTION", action_skipped_reason="NO_ACTION",
        )],
    )
    monkeypatch.setattr(api_app, "run_reconciliation_pipeline", lambda *a, **k: result)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 200
    body = response.json()
    assert body["events"][0]["decision_type"] == "NO_ACTION"
    assert body["events"][0]["action_id"] is None
    assert body["events"][0]["action_status"] is None


def test_reconcile_approval_pending_is_reported_and_not_auto_approved(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "RazorpayReadClient", lambda: _FakeReadClient())
    result = PipelineRunResult(
        order_id="order_x", new_event_count=1,
        events=[EventProcessingResult(
            event_id="e1", event_type="payment.attempt.authorized",
            decision_id="d1", decision_type="RECOMMEND_CAPTURE",
            action_id="a1", action_status="APPROVAL_PENDING",
        )],
    )
    monkeypatch.setattr(api_app, "run_reconciliation_pipeline", lambda *a, **k: result)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 200
    assert response.json()["events"][0]["action_status"] == "APPROVAL_PENDING"
    assert not hasattr(api_app, "grant_approval")
    assert not hasattr(api_app, "reject_approval")


def test_reconcile_missing_merchant_returns_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: None)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 404


def test_reconcile_razorpay_failure_returns_502(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "RazorpayReadClient", lambda: _FakeReadClient())

    def _raise(*a, **k):
        raise RazorpayAPIError("Razorpay API returned HTTP 404")

    monkeypatch.setattr(api_app, "run_reconciliation_pipeline", _raise)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 502
    assert "credential" not in response.json()["detail"].lower()


def test_reconcile_unresolved_event_returns_500(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "RazorpayReadClient", lambda: _FakeReadClient())

    def _raise(*a, **k):
        raise UnresolvedEventError("could not resolve")

    monkeypatch.setattr(api_app, "run_reconciliation_pipeline", _raise)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 500


def test_reconcile_missing_credentials_returns_500_without_leaking_detail(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))

    def _raise_runtime_error():
        raise RuntimeError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set as environment variables")

    monkeypatch.setattr(api_app, "RazorpayReadClient", _raise_runtime_error)

    response = client.post(f"/merchants/{_VALID_MERCHANT_ID}/orders/order_x/reconcile")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "RAZORPAY_KEY_ID" not in detail
    assert "RAZORPAY_KEY_SECRET" not in detail


class _FakeReadClient:
    def close(self):
        pass


# ---------------------------------------------------------------------------
# Observability endpoint -- merchant isolation
# ---------------------------------------------------------------------------


def test_metrics_endpoint_preserves_merchant_scoping(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    calls = {}

    def _tracked(name):
        def _fn(conn, merchant_id):
            calls[name] = merchant_id
            return _EMPTY_REPORTS[name]
        return _fn

    _EMPTY_REPORTS = {
        "decision_type_distribution": DecisionTypeDistribution(merchant_id=_VALID_MERCHANT_ID, counts={}),
        "policy_outcome_distribution": PolicyOutcomeDistribution(merchant_id=_VALID_MERCHANT_ID, allow=0, approval_required=0, block=0),
        "capture_terminal_status_distribution": CaptureTerminalStatusDistribution(merchant_id=_VALID_MERCHANT_ID, verified_success=0, verified_failed=0, escalated=0, blocked=0),
        "escalation_metrics": EscalationMetrics(merchant_id=_VALID_MERCHANT_ID, total_escalated=0, by_reason={}),
        "verification_read_attempt_distribution": VerificationReadAttemptDistribution(merchant_id=_VALID_MERCHANT_ID, by_attempt_count={}),
        "verified_captured_amount": VerifiedCapturedAmount(merchant_id=_VALID_MERCHANT_ID, verified_success_count=0, total_verified_captured_amount=0),
        "verification_resolution_timing": VerificationResolutionTiming(merchant_id=_VALID_MERCHANT_ID, count=0, min_seconds=None, max_seconds=None, avg_seconds=None),
        "retry_prompt_outcome_availability": RetryPromptOutcomeAvailability(merchant_id=_VALID_MERCHANT_ID, total_customer_retry_prompt_actions=0),
    }

    for name in _EMPTY_REPORTS:
        monkeypatch.setattr(api_app, name, _tracked(name))

    response = client.get(f"/merchants/{_VALID_MERCHANT_ID}/metrics")

    assert response.status_code == 200
    for name in _EMPTY_REPORTS:
        assert calls[name] == _VALID_MERCHANT_ID
    body = response.json()
    assert "accuracy" not in str(body).lower()


def test_metrics_missing_merchant_returns_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: None)

    response = client.get(f"/merchants/{_VALID_MERCHANT_ID}/metrics")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------


def test_responses_never_contain_credential_like_values(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "list_orders_for_merchant", lambda conn, mid: [])

    fake_secret = "sk_test_definitely_not_a_real_secret_value_12345"
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", fake_secret)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:definitely_not_real@host/db")

    response = client.get(f"/merchants/{_VALID_MERCHANT_ID}/payments")

    assert fake_secret not in response.text
    assert "definitely_not_real" not in response.text
