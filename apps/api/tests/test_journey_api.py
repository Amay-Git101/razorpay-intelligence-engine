"""HTTP contract for the four guided problem journeys.

The security tests here are the important ones. Everything else in this
file checks shapes; `test_checkout_config_*` checks that the one piece of
Razorpay configuration the browser is given cannot be the wrong one.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import api.app as api_app
from api.app import app, get_db

client = TestClient(app)


class _FakeConn:
    pass


def _override_get_db():
    yield _FakeConn()


app.dependency_overrides[get_db] = _override_get_db

_VALID_MERCHANT_ID = str(uuid.uuid4())
_VALID_EXPERIMENT_ID = str(uuid.uuid4())


def _merchant_row(merchant_id: str = _VALID_MERCHANT_ID) -> dict:
    import datetime

    return {"id": merchant_id, "name": "Journey Merchant", "created_at": datetime.datetime(2026, 1, 1)}


# ---------------------------------------------------------------------------
# GET /checkout-config -- the browser's only Razorpay configuration
# ---------------------------------------------------------------------------


def test_checkout_config_returns_only_the_publishable_key(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_publishable123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "super_secret_value_do_not_leak")

    response = client.get("/checkout-config")

    assert response.status_code == 200
    body = response.json()
    assert body == {"key_id": "rzp_test_publishable123", "mode": "test"}
    assert "super_secret_value_do_not_leak" not in response.text


def test_checkout_config_refuses_to_serve_a_live_key(monkeypatch):
    """A live key in the browser would let a visitor start real payments
    with real money. The endpoint fails closed rather than serving it."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_realkey123")

    response = client.get("/checkout-config")

    assert response.status_code == 500
    assert "rzp_live_realkey123" not in response.text


def test_checkout_config_reports_missing_configuration(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)

    response = client.get("/checkout-config")

    assert response.status_code == 500


def test_the_secret_never_appears_in_any_journey_response(monkeypatch):
    """A blunt check across the endpoints the browser actually calls."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_publishable123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "another_secret_that_must_not_travel")
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setattr(api_app, "list_merchants", lambda conn: [_merchant_row()])

    for response in (client.get("/checkout-config"), client.get("/merchants"), client.get("/health")):
        assert "another_secret_that_must_not_travel" not in response.text


# ---------------------------------------------------------------------------
# POST /merchants/{id}/test-orders
# ---------------------------------------------------------------------------


def test_creating_orders_for_a_missing_merchant_is_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: None)

    response = client.post(
        f"/merchants/{_VALID_MERCHANT_ID}/test-orders",
        json={"kind": "failure_pattern", "count": 6, "amount": 50000},
    )

    assert response.status_code == 404


def test_a_cohort_larger_than_the_ceiling_is_rejected_by_the_schema():
    """The bound is enforced before any Razorpay call is made."""
    response = client.post(
        f"/merchants/{_VALID_MERCHANT_ID}/test-orders",
        json={"kind": "failure_pattern", "count": 50, "amount": 50000},
    )

    assert response.status_code == 422


def test_a_zero_amount_is_rejected_by_the_schema():
    response = client.post(
        f"/merchants/{_VALID_MERCHANT_ID}/test-orders",
        json={"kind": "capture_decision", "count": 1, "amount": 0},
    )

    assert response.status_code == 422


def test_a_malformed_merchant_id_is_400():
    response = client.post(
        "/merchants/not-a-uuid/test-orders",
        json={"kind": "capture_decision", "count": 1, "amount": 50000},
    )

    assert response.status_code == 400


def test_an_unknown_experiment_kind_is_400(monkeypatch):
    monkeypatch.setattr(api_app, "get_merchant", lambda conn, mid: _merchant_row(mid))
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")

    class _NeverCalledClient:
        def create_order(self, *a, **k):
            raise AssertionError("Razorpay must not be called for an invalid kind")

        def close(self):
            pass

    monkeypatch.setattr(api_app, "RazorpayOrderClient", lambda *a, **k: _NeverCalledClient())

    response = client.post(
        f"/merchants/{_VALID_MERCHANT_ID}/test-orders",
        json={"kind": "not_a_journey", "count": 1, "amount": 50000},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /experiments/{id}
# ---------------------------------------------------------------------------


def test_experiment_detail_reports_unpaid_orders_as_absent_not_defaulted(monkeypatch):
    """An order nobody has paid has null payment fields. It is never given a
    status that was not observed."""
    import datetime

    monkeypatch.setattr(
        api_app, "get_experiment",
        lambda conn, eid: {
            "id": eid, "merchant_id": _VALID_MERCHANT_ID, "kind": "failure_pattern",
            "source": "razorpay_test_mode", "label": None,
            "created_at": datetime.datetime(2026, 1, 1),
        },
    )
    monkeypatch.setattr(
        api_app, "list_experiment_orders_with_state",
        lambda conn, eid: [
            {
                "position": 1, "order_id": "order_a", "amount": 50000, "currency": "INR",
                "order_status": "created", "payment_attempt_id": None, "payment_status": None,
                "payment_captured": None, "payment_method": None, "error_reason": None,
                "error_step": None, "error_source": None, "payment_observed_at": None,
            }
        ],
    )

    response = client.get(f"/experiments/{_VALID_EXPERIMENT_ID}")

    assert response.status_code == 200
    order = response.json()["orders"][0]
    assert order["payment_status"] is None
    assert order["payment_captured"] is None


def test_a_missing_experiment_is_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_experiment", lambda conn, eid: None)

    assert client.get(f"/experiments/{_VALID_EXPERIMENT_ID}").status_code == 404
    assert client.get(f"/experiments/{_VALID_EXPERIMENT_ID}/failure-pattern").status_code == 404


# ---------------------------------------------------------------------------
# GET /payments/{id}/customer-history
# ---------------------------------------------------------------------------


def test_customer_history_distinguishes_no_identity_from_no_history(monkeypatch):
    """These are different facts and the API keeps them apart."""
    import datetime

    monkeypatch.setattr(
        api_app, "get_payment_attempt",
        lambda conn, pid: {
            "id": pid, "order_id": "order_a", "raw_reference": {"id": pid},
            "observed_at": datetime.datetime(2026, 1, 1),
        },
    )
    monkeypatch.setattr(api_app, "get_order", lambda conn, oid: {"merchant_id": _VALID_MERCHANT_ID})
    monkeypatch.setattr(api_app, "summarize_customer_history", lambda *args, **kwargs: None)

    response = client.get("/payments/pay_synthetic/customer-history")

    assert response.status_code == 200
    body = response.json()
    assert body["identity_available"] is False
    assert body["history"] is None


def test_customer_history_for_a_missing_payment_is_404(monkeypatch):
    monkeypatch.setattr(api_app, "get_payment_attempt", lambda conn, pid: None)

    assert client.get("/payments/pay_missing/customer-history").status_code == 404
