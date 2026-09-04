"""The exact input a diagnosis model is allowed to see.

THE OMISSION IS THE DESIGN. FailureSignals has no amount, no currency, no
merchant policy limit, and no customer identity. This is not an oversight
and it is not merely a privacy measure -- it is the reason the AI layer
cannot be steered into moving money.

The model classifies a failure mode from failure evidence. It is never
told what the payment is worth, so it cannot prefer an aggressive
recommendation for a large payment, and a prompt-injection payload
smuggled into a bank's error string cannot reference an amount the model
was never given. Whether an amount is within a merchant's limit is
computed later, by policy/rules.py, from the RAW persisted amount.

build_failure_signals() is the ONLY constructor used in production. It
takes the full payment-attempt row and copies across a fixed allowlist of
keys, so adding a column to payment_attempts can never silently widen
what the model sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# The complete set of payment-attempt facts a diagnosis may be built from.
# Enforced by build_failure_signals() and asserted by a test.
ALLOWED_SIGNAL_KEYS: frozenset[str] = frozenset(
    {"error_code", "error_description", "error_source", "error_step", "error_reason", "method", "bank", "wallet"}
)


class FailureSignals(BaseModel):
    """Failure evidence only. See module docstring for what is missing and why."""

    # extra="forbid", not pydantic's default of "ignore". An amount passed
    # here must be a loud error, not a silently dropped keyword -- silently
    # dropping it would let a caller believe it had supplied context the
    # model would use, and would let a future refactor start passing one
    # without anything failing.
    model_config = ConfigDict(extra="forbid")

    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    method: str | None = None
    bank: str | None = None
    wallet: str | None = None
    # Derived, not raw: how many times this order has already been attempted.
    # A count, never an amount -- it is the one piece of history that changes
    # whether another attempt is sensible.
    prior_attempt_count: int = 0


def build_failure_signals(payment_attempt: dict[str, Any], prior_attempt_count: int = 0) -> FailureSignals:
    """Project a payment_attempts row onto the allowlist.

    Reads error_* / method from the row's top level and falls back to the
    Razorpay raw_reference payload for fields the table has no column for
    (error_code, bank, wallet). Anything not in ALLOWED_SIGNAL_KEYS is
    dropped -- including amount, which is present in every row and must
    never reach the model.
    """
    raw = payment_attempt.get("raw_reference") or {}
    merged = {
        key: (payment_attempt[key] if payment_attempt.get(key) is not None else raw.get(key))
        for key in ALLOWED_SIGNAL_KEYS
    }
    return FailureSignals(**merged, prior_attempt_count=prior_attempt_count)
