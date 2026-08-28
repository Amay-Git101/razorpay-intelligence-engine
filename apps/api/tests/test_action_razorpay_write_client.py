"""RazorpayWriteClient tests. Pure Python + mocked HTTP transport, no
network, no DATABASE_URL. Mirrors the read-client test pattern from
Gate 3 (test_razorpay_client.py)."""

from __future__ import annotations

import httpx
import pytest

from action.razorpay_write_client import RazorpayWriteClient
from razorpay_client.errors import RazorpayAPIError


def _client_with_handler(handler) -> RazorpayWriteClient:
    return RazorpayWriteClient(
        key_id="rzp_test_fake_key_id",
        key_secret="fake_secret_never_real",
        transport=httpx.MockTransport(handler),
    )


def test_capture_payment_sends_correct_path_method_body_and_auth():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["auth_header"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "pay_TEST123", "status": "captured", "captured": True})

    client = _client_with_handler(handler)
    result = client.capture_payment("pay_TEST123", 50000)

    assert captured["path"] == "/v1/payments/pay_TEST123/capture"
    assert captured["method"] == "POST"
    assert str(captured["auth_header"]).startswith("Basic ")
    assert b"50000" in captured["body"]
    assert result["status"] == "captured"


def test_non_200_response_raises_with_status_code_and_no_credential_leak():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"description": "Amount mismatch"}})

    client = _client_with_handler(handler)
    with pytest.raises(RazorpayAPIError) as exc_info:
        client.capture_payment("pay_TEST123", 50000)

    assert exc_info.value.status_code == 400
    assert "fake_secret_never_real" not in str(exc_info.value)


def test_transport_error_propagates_uncaught_as_httpx_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    client = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPError):
        client.capture_payment("pay_TEST123", 50000)


def test_missing_credentials_raises_clear_error(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        RazorpayWriteClient()
