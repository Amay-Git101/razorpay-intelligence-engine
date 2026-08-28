"""Razorpay WRITE adapter. Exactly one capability: capture an
already-authorized payment (POST /v1/payments/:id/capture) -- the single
VERIFIED write endpoint (Phase 1 step 9: order_TV4zd1gEZHQRZ7 /
pay_TV530e8hTSjpC8). No other write method exists on this class -- not
refund, not payment links, not even as a stub.

Only src/action/* may import this module. Enforced two ways: by
placement (nested inside the action package, not the shared
razorpay_client read package) and by a source-scanning test
(tests/test_architecture_boundaries.py) that fails if any other module
imports it.

Transport-level failures (timeout, connection error) are deliberately
NOT caught here -- they propagate as raw httpx exceptions so the caller
(action/orchestrator.py) can distinguish "no response was ever received"
(ambiguous) from "a definite non-2xx response" (RazorpayAPIError,
raised below, carrying status_code). We have no VERIFIED evidence of
what a rejected capture call's error body looks like -- Phase 1 only
exercised the success path -- so no specific error schema is assumed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from razorpay_client.errors import RazorpayAPIError

BASE_URL = "https://api.razorpay.com/v1"


class RazorpayWriteClient:
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
        self._client = httpx.Client(
            base_url=base_url, auth=(key_id, key_secret), timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RazorpayWriteClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def capture_payment(self, payment_id: str, amount: int, currency: str = "INR") -> dict[str, Any]:
        # No exception handling here for transport errors -- see module
        # docstring. Only a definite HTTP response is converted to
        # RazorpayAPIError.
        response = self._client.post(
            f"/payments/{payment_id}/capture", json={"amount": amount, "currency": currency}
        )
        if response.status_code != 200:
            raise RazorpayAPIError(
                f"Razorpay API returned HTTP {response.status_code} for capture of {payment_id}",
                status_code=response.status_code,
            )
        return response.json()
