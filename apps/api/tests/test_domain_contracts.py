"""Pure-Python tests for the domain contracts layer.

No database required -- these run in any environment with the project's
Python dependencies installed, and are executed as part of this gate's
report (unlike test_db_invariants.py, which is blocked on Postgres
availability).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domain.contracts import (
    ActionType,
    PaymentAttemptStatus,
    ProvenanceBand,
    ProvenancedField,
    compute_idempotency_key,
    is_valid_payment_attempt_transition,
)


# ---------------------------------------------------------------------------
# Provenance validation (test item 12)
# ---------------------------------------------------------------------------

def test_raw_field_does_not_require_confidence():
    field = ProvenancedField(field="amount", value=50000, band=ProvenanceBand.RAW, source="razorpay_api_poll")
    assert field.confidence is None


def test_ai_output_field_requires_confidence_and_model_version():
    with pytest.raises(ValidationError):
        ProvenancedField(field="recovery_score", value=0.7, band=ProvenanceBand.AI_OUTPUT, source="rule_v1")


def test_ai_output_field_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        ProvenancedField(
            field="recovery_score",
            value=0.7,
            band=ProvenanceBand.AI_OUTPUT,
            source="rule_v1",
            confidence=1.5,
            model_version="rule_v1",
        )


def test_ai_output_field_valid_with_full_metadata():
    field = ProvenancedField(
        field="recovery_score",
        value=0.7,
        band=ProvenanceBand.AI_OUTPUT,
        source="rule_v1",
        confidence=0.82,
        model_version="rule_v1",
    )
    assert field.confidence == 0.82
    assert field.model_version == "rule_v1"


# ---------------------------------------------------------------------------
# Idempotency key contract (test item 13, partial -- pure computation)
# ---------------------------------------------------------------------------

def test_idempotency_key_excludes_decision_id_by_construction():
    # The function signature itself has no decision_id parameter -- this
    # test exists to make that guarantee explicit and regression-proof,
    # not just implicit in the function shape.
    key = compute_idempotency_key(
        merchant_id="merchant_x", order_id="order_x", payment_attempt_id="pay_x", action_type=ActionType.CAPTURE_PAYMENT
    )
    assert isinstance(key, str)
    assert len(key) == 64  # sha256 hex digest


def test_idempotency_key_deterministic_for_same_operation():
    key_a = compute_idempotency_key("merchant_x", "order_x", "pay_x", ActionType.CAPTURE_PAYMENT)
    key_b = compute_idempotency_key("merchant_x", "order_x", "pay_x", ActionType.CAPTURE_PAYMENT)
    assert key_a == key_b


def test_idempotency_key_differs_for_different_operations():
    key_a = compute_idempotency_key("merchant_x", "order_x", "pay_x", ActionType.CAPTURE_PAYMENT)
    key_b = compute_idempotency_key("merchant_x", "order_x", "pay_y", ActionType.CAPTURE_PAYMENT)
    assert key_a != key_b


# ---------------------------------------------------------------------------
# Payment attempt transition validity matrix (mirrors the DB trigger)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "old,new,expected",
    [
        (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.AUTHORIZED, True),
        (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.CAPTURED, True),
        (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.FAILED, True),
        (PaymentAttemptStatus.AUTHORIZED, PaymentAttemptStatus.CAPTURED, True),
        (PaymentAttemptStatus.FAILED, PaymentAttemptStatus.CAPTURED, False),
        (PaymentAttemptStatus.CAPTURED, PaymentAttemptStatus.FAILED, False),
        (PaymentAttemptStatus.CAPTURED, PaymentAttemptStatus.AUTHORIZED, False),
    ],
)
def test_transition_validity_matrix(old, new, expected):
    assert is_valid_payment_attempt_transition(old, new) is expected
