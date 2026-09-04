"""The model must never be shown what a payment is worth.

This is the single most important test in the AI layer. The safety argument
for putting a language model anywhere near a payments system rests on the
model classifying a failure without knowing the stakes -- so that no prompt,
no injected error string, and no future edit can make it recommend more
aggressively for a large payment than a small one.

That argument is only true if it is mechanically enforced. These tests
enforce it two ways: the allowlist is checked directly, and a payment row
stuffed with every money-shaped field we could think of is passed through the
real constructor to confirm none of them survive.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from diagnosis.diagnoser import _render_evidence
from diagnosis.signals import ALLOWED_SIGNAL_KEYS, FailureSignals, build_failure_signals

# Anything that could tell the model how much money is involved, or who the
# customer is. If a future change adds one of these to the allowlist, this
# test fails and the change has to be argued for explicitly.
FORBIDDEN_SUBSTRINGS = ("amount", "currency", "value", "price", "total", "email", "contact", "customer_id", "notes")


def test_allowlist_contains_no_money_or_identity_fields():
    for key in ALLOWED_SIGNAL_KEYS:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in key, f"signal key {key!r} looks like a {forbidden} field"


def test_failure_signals_model_has_no_amount_field():
    assert "amount" not in FailureSignals.model_fields
    assert "currency" not in FailureSignals.model_fields


def test_failure_signals_rejects_an_amount_passed_explicitly():
    # Pydantic's default is to ignore unknown fields; this asserts the model
    # is configured strictly enough that smuggling an amount in is an error,
    # not a silent no-op that later gets rendered into the prompt.
    with pytest.raises((ValidationError, TypeError)):
        FailureSignals(error_reason="insufficient_funds", amount=15_000_000)


def test_build_failure_signals_drops_every_money_field_from_a_real_row():
    payment_attempt = {
        "id": "pay_TEST123",
        "order_id": "order_TEST123",
        "status": "failed",
        "amount": 15_000_000,           # Rs 1,50,000 -- must not survive
        "captured": False,
        "error_source": "issuer",
        "error_step": "payment_authorization",
        "error_reason": "card_blocked",
        "method": "card",
        "raw_reference": {
            "amount": 15_000_000,
            "currency": "INR",
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "This card is blocked for online transactions.",
            "bank": "KKBK",
            "wallet": None,
            "email": "customer@example.com",
            "contact": "+919999999999",
        },
    }

    signals = build_failure_signals(payment_attempt, prior_attempt_count=1)

    dumped = signals.model_dump()
    assert "amount" not in dumped
    assert "currency" not in dumped
    assert "email" not in dumped

    # The evidence actually sent to the model is the real test: no
    # representation of the amount may appear anywhere in the rendered prompt.
    evidence = _render_evidence(signals)
    assert "15000000" not in evidence
    assert "150000" not in evidence
    assert "INR" not in evidence
    assert "customer@example.com" not in evidence

    # ...while the failure evidence it legitimately needs is present.
    assert "card_blocked" in evidence
    assert "KKBK" in evidence
    assert "blocked for online transactions" in evidence
    assert "prior_attempt_count" in evidence


def test_build_failure_signals_falls_back_to_raw_reference_for_columns_that_do_not_exist():
    # error_code, bank and wallet have no column on payment_attempts; they
    # live only in the Razorpay payload. Losing them would blind the model to
    # most of the useful evidence, so the fallback is load-bearing.
    signals = build_failure_signals(
        {
            "status": "failed",
            "error_source": None,
            "raw_reference": {
                "error_code": "GATEWAY_ERROR",
                "error_source": "gateway",
                "bank": "SBIN",
                "wallet": "payzapp",
            },
        }
    )
    assert signals.error_code == "GATEWAY_ERROR"
    assert signals.error_source == "gateway"   # column was NULL, payload had it
    assert signals.bank == "SBIN"
    assert signals.wallet == "payzapp"
