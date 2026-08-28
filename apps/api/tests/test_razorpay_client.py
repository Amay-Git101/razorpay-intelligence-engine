"""RazorpayReadClient tests. Pure Python + mocked HTTP transport -- no
network, no live Razorpay credentials required, no DATABASE_URL
required. Fixtures are minimal (this file tests URL/auth/error-handling
mechanics, not response-shape correctness -- that's covered with
Phase-1-shaped fixtures in test_reconciliation.py).
"""

from __future__ import annotations

import httpx
import pytest

from razorpay_client.client import RazorpayReadClient
from razorpay_client.errors import RazorpayAPIError


def _client_with_handler(handler) -> RazorpayReadClient:
    return RazorpayReadClient(
        key_id="rzp_test_fake_key_id",
        key_secret="fake_secret_never_real",
        transport=httpx.MockTransport(handler),
    )


def test_get_order_sends_correct_path_and_basic_auth():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth_header"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "order_TEST123", "status": "created"})

    client = _client_with_handler(handler)
    result = client.get_order("order_TEST123")

    assert captured["path"] == "/v1/orders/order_TEST123"
    assert captured["auth_header"] is not None
    assert captured["auth_header"].startswith("Basic ")
    assert result["id"] == "order_TEST123"


def test_get_order_payments_returns_items_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/orders/order_TEST123/payments"
        return httpx.Response(
            200, json={"entity": "collection", "count": 2, "items": [{"id": "pay_a"}, {"id": "pay_b"}]}
        )

    client = _client_with_handler(handler)
    result = client.get_order_payments("order_TEST123")
    assert [p["id"] for p in result] == ["pay_a", "pay_b"]


def test_get_payment_sends_correct_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "pay_TEST123", "status": "captured"})

    client = _client_with_handler(handler)
    client.get_payment("pay_TEST123")


def test_non_200_response_raises_without_leaking_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"description": "Authentication failed"}})

    client = _client_with_handler(handler)
    with pytest.raises(RazorpayAPIError) as exc_info:
        client.get_order("order_TEST123")

    message = str(exc_info.value)
    assert "fake_secret_never_real" not in message
    assert "401" in message


def test_missing_credentials_raises_clear_error(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        RazorpayReadClient()
