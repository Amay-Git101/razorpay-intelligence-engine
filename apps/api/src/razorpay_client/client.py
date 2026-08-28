"""Strictly read-only Razorpay API client.

Implements exactly the three VERIFIED-capability endpoints from Phase 1
hands-on verification (GET /v1/orders/:id, GET /v1/orders/:id/payments,
GET /v1/payments/:id). No write methods exist here -- not capture, not
refund, nothing. Per the approved architecture, only the (future) Action
module may invoke a Razorpay write endpoint, and that code deliberately
does not live in this class.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from .errors import RazorpayAPIError

BASE_URL = "https://api.razorpay.com/v1"


class RazorpayReadClientProtocol(Protocol):
    """Structural interface used by the reconciliation service, so tests
    can inject a fake client without inheriting from RazorpayReadClient."""

    def get_order(self, order_id: str) -> dict[str, Any]: ...
    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]: ...
    def get_payment(self, payment_id: str) -> dict[str, Any]: ...


class RazorpayReadClient:
    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key_id = key_id or os.environ.get("RAZORPAY_KEY_ID")
        key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set as environment "
                "variables (or passed explicitly). Never hardcode these values."
            )
        # httpx's tuple auth is Basic Auth -- matches the VERIFIED
        # `curl -u KEY_ID:KEY_SECRET` pattern from Phase 1 step 1.
        self._client = httpx.Client(
            base_url=base_url, auth=(key_id, key_secret), timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RazorpayReadClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            # Deliberately generic: never echo the underlying exception's
            # request/response objects, which could carry auth headers.
            raise RazorpayAPIError(f"request to Razorpay failed: {type(exc).__name__}") from None

        if response.status_code != 200:
            raise RazorpayAPIError(f"Razorpay API returned HTTP {response.status_code} for {path}")
        return response.json()

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._get(f"/orders/{order_id}")

    def get_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/orders/{order_id}/payments")
        return data.get("items", [])

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return self._get(f"/payments/{payment_id}")
