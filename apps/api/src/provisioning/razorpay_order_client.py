"""Razorpay ORDER-CREATION adapter. Exactly one capability: create an
order (POST /v1/orders).

WHY THIS IS NOT IN action/ AND IS NOT THE CAPTURE ADAPTER
Creating an order moves no money. An order is a request-for-payment
object: it holds an amount and a receipt, and until a customer completes
Checkout against it nothing has been charged, authorized, or captured.
The money-moving boundary in this system is capture, and that lives in
the action package behind Policy and Verification. Putting order creation
there would blur the one boundary the whole architecture is organised
around, so it is a separate adapter in its own package with its own
single capability.

This class has NO capture method, NO refund method, and NO payout
method -- not even as a stub. It cannot take money under any code path,
which is a structural fact about the file rather than a promise.
Enforced by tests/test_architecture_boundaries.py.

PAYMENT_CAPTURE IS ALWAYS 0, AND THAT IS LOAD-BEARING
Every order created here sets payment_capture=0, so a completed payment
lands in `authorized` with `captured=false` and waits for this system to
decide. That is the whole premise of the capture journey: if Razorpay
auto-captured on payment, there would be no decision left to make, and
the pipeline would only ever observe an already-finished payment. An
earlier order in this project's history was created without this
parameter, auto-captured before reconciliation ever saw it, and produced
exactly that dead end -- see the failure-recovery writeup. Setting it
explicitly here is the fix for that incident, applied at the only place
that can prevent it.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from razorpay_client.errors import RazorpayAPIError

BASE_URL = "https://api.razorpay.com/v1"

# Razorpay's manual-capture flag on order creation. 0 = the gateway must
# NOT auto-capture an authorized payment; this system decides instead.
MANUAL_CAPTURE = 0


class RazorpayOrderClient:
    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
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

    def __enter__(self) -> "RazorpayOrderClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_order(
        self,
        amount: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Creates one order with manual capture. Returns Razorpay's own
        order object verbatim -- it is stored as the order's
        raw_reference, so what this system persists is what Razorpay
        actually said, not a reshaped copy of it."""
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "payment_capture": MANUAL_CAPTURE,
        }
        if receipt is not None:
            payload["receipt"] = receipt
        if notes:
            payload["notes"] = notes

        response = self._client.post("/orders", json=payload)
        if response.status_code not in (200, 201):
            raise RazorpayAPIError(
                f"Razorpay API returned HTTP {response.status_code} for order creation",
                status_code=response.status_code,
            )
        return response.json()
