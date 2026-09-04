"""Customer payment history as decision context.

Three separable claims are tested here:

  1. The history is REAL -- derived from the Razorpay payment object this
     system already stores, not from anything invented for the demo. A
     payment with no identity gets no history, not a blank profile.
  2. The identity is NOT copied into the persisted decision context. Only
     an opaque fingerprint is, because decisions.context_snapshot is
     written on every decision and read widely.
  3. History can only ever move a decision TOWARD human review. This is
     the safety property: a payer's past cannot buy them more automation
     than a payer with no history at all.
"""

from __future__ import annotations

import uuid

import pytest

from context.customer_history import (
    extract_customer_identity,
    summarize_customer_history,
)
from domain.contracts import (
    ContextSnapshot,
    DecisionType,
    Diagnosis,
    Expectation,
    FailureClass,
    ProvenanceBand,
    ProvenancedField,
    RootCause,
)
from intelligence.recovery_engine import RecoveryEngine
from repository.merchants import insert_merchant
from repository.orders import upsert_order
from repository.payment_attempts import insert_payment_attempt

CUSTOMER_EMAIL = "repeat.payer@example.com"


def _merchant(db_conn) -> str:
    return str(insert_merchant(db_conn, "History Test Merchant", {}, {}))


def _order(db_conn, merchant_id: str, amount: int = 50000) -> str:
    order_id = f"order_HIS_{uuid.uuid4().hex[:10]}"
    upsert_order(
        db_conn, order_id=order_id, merchant_id=merchant_id, amount=amount,
        amount_paid=0, amount_due=amount, status="attempted", attempts=1,
        currency="INR", raw_reference={"test": True},
    )
    return order_id


def _attempt(
    db_conn,
    order_id: str,
    status: str = "failed",
    captured: bool = False,
    email: str | None = CUSTOMER_EMAIL,
    amount: int = 50000,
) -> str:
    payment_id = f"pay_HIS_{uuid.uuid4().hex[:10]}"
    raw: dict = {"id": payment_id, "entity": "payment"}
    if email is not None:
        raw["email"] = email
    insert_payment_attempt(
        db_conn, payment_attempt_id=payment_id, order_id=order_id, status=status,
        method="card", captured=captured,
        error_source="bank" if status == "failed" else None,
        error_step="payment_authorization" if status == "failed" else None,
        error_reason="payment_failed" if status == "failed" else None,
        amount=amount, raw_reference=raw,
    )
    return payment_id


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------


def test_identity_comes_from_the_stored_razorpay_payment_object():
    identity = extract_customer_identity({"email": "someone@example.com", "contact": "+919000090000"})
    assert identity is not None
    assert identity.kind == "email"


def test_contact_is_used_when_no_email_is_present():
    identity = extract_customer_identity({"contact": "+919000090000"})
    assert identity is not None
    assert identity.kind == "contact"


def test_a_payment_with_no_identity_has_none_rather_than_a_blank_profile():
    """Synthetic rows carry no email or contact. The honest answer is 'no
    identity', never a default customer."""
    assert extract_customer_identity({"id": "pay_SYN_x", "entity": "payment"}) is None
    assert extract_customer_identity(None) is None
    assert extract_customer_identity({"email": "   "}) is None


def test_the_fingerprint_does_not_contain_the_address():
    identity = extract_customer_identity({"email": CUSTOMER_EMAIL})
    assert identity is not None
    assert CUSTOMER_EMAIL not in identity.fingerprint
    assert "repeat.payer" not in identity.fingerprint


def test_the_fingerprint_is_stable_and_case_insensitive():
    a = extract_customer_identity({"email": "Payer@Example.com"})
    b = extract_customer_identity({"email": " payer@example.com "})
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_an_email_and_a_contact_with_the_same_text_do_not_collide():
    same = "9000090000"
    email_identity = extract_customer_identity({"email": same})
    contact_identity = extract_customer_identity({"contact": same})
    assert email_identity is not None and contact_identity is not None
    assert email_identity.fingerprint != contact_identity.fingerprint


# ---------------------------------------------------------------------------
# History summarisation, against a real database
# ---------------------------------------------------------------------------


def test_prior_payments_by_the_same_payer_are_counted(db_conn):
    merchant_id = _merchant(db_conn)
    _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)
    _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True)
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    current_order = _order(db_conn, merchant_id)
    current_payment = _attempt(db_conn, current_order, status="failed")

    history = summarize_customer_history(
        db_conn, merchant_id, current_payment, {"email": CUSTOMER_EMAIL}
    )

    assert history is not None
    assert history.prior_payment_count == 3
    assert history.prior_captured_count == 2
    assert history.prior_failed_count == 1


def test_the_payment_being_judged_is_excluded_from_its_own_history(db_conn):
    merchant_id = _merchant(db_conn)
    current_payment = _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    history = summarize_customer_history(
        db_conn, merchant_id, current_payment, {"email": CUSTOMER_EMAIL}
    )

    assert history is not None
    assert history.prior_payment_count == 0


def test_another_payers_payments_are_not_counted(db_conn):
    merchant_id = _merchant(db_conn)
    for _ in range(3):
        _attempt(db_conn, _order(db_conn, merchant_id), status="captured", captured=True, email="other@example.com")

    current_payment = _attempt(db_conn, _order(db_conn, merchant_id), status="failed")
    history = summarize_customer_history(
        db_conn, merchant_id, current_payment, {"email": CUSTOMER_EMAIL}
    )

    assert history is not None
    assert history.prior_payment_count == 0


def test_another_merchants_payments_are_not_counted(db_conn):
    """The same person paying two different merchants has two separate
    histories. Merging them would leak one merchant's data into another's
    decision."""
    merchant_a = _merchant(db_conn)
    merchant_b = _merchant(db_conn)
    for _ in range(3):
        _attempt(db_conn, _order(db_conn, merchant_b), status="failed")

    current_payment = _attempt(db_conn, _order(db_conn, merchant_a), status="failed")
    history = summarize_customer_history(
        db_conn, merchant_a, current_payment, {"email": CUSTOMER_EMAIL}
    )

    assert history is not None
    assert history.prior_payment_count == 0


def test_a_payment_without_identity_yields_no_history(db_conn):
    merchant_id = _merchant(db_conn)
    current_payment = _attempt(db_conn, _order(db_conn, merchant_id), status="failed", email=None)

    assert summarize_customer_history(db_conn, merchant_id, current_payment, {"id": current_payment}) is None


# ---------------------------------------------------------------------------
# What the history is allowed to change
# ---------------------------------------------------------------------------


def _failed_context(**history_fields) -> ContextSnapshot:
    fields = [
        ProvenancedField(field="amount", value=50000, band=ProvenanceBand.RAW, source="razorpay_api_poll"),
        ProvenancedField(field="status", value="failed", band=ProvenanceBand.RAW, source="razorpay_api_poll"),
        ProvenancedField(field="prior_attempt_count", value=0, band=ProvenanceBand.DERIVED, source="internal_count"),
    ]
    for name, value in history_fields.items():
        fields.append(
            ProvenancedField(field=name, value=value, band=ProvenanceBand.DERIVED, source="customer_history:email")
        )
    return ContextSnapshot(order_id="order_x", payment_attempt_id="pay_x", fields=fields)


def _retryable_diagnosis() -> Diagnosis:
    return Diagnosis(
        root_cause=RootCause.ISSUER_DECLINE_TEMPORARY,
        failure_class=FailureClass.TRANSIENT,
        retry_advisable=True,
        confidence=0.9,
        rationale="temporary issuer decline; retrying is reasonable",
        model_version="test/diagnosis_v1",
    )


def _expectation() -> Expectation:
    return Expectation(bucket_key="test", expected_recovery_rate=0.5, sample_size=10, source="test")


def test_repeated_customer_failures_send_a_retryable_payment_to_a_human():
    """The decision changes because of observed history -- this is the
    point of the feature."""
    output = RecoveryEngine(customer_failure_escalation_threshold=3).evaluate(
        _failed_context(customer_prior_failed_count=4, customer_prior_captured_count=0),
        _expectation(),
        diagnosis=_retryable_diagnosis(),
    )

    assert output.decision_type == DecisionType.RECOMMEND_ESCALATION
    assert "CUSTOMER_HISTORY_REPEATED_FAILURES" in output.reason_codes


def test_the_same_payment_without_that_history_is_still_automated():
    """Identical payment, identical diagnosis, no history: the original
    behaviour is unchanged. This is what makes the rule additive."""
    output = RecoveryEngine().evaluate(_failed_context(), _expectation(), diagnosis=_retryable_diagnosis())

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT


def test_a_payer_with_prior_successes_is_not_escalated_by_the_rule():
    output = RecoveryEngine(customer_failure_escalation_threshold=3).evaluate(
        _failed_context(customer_prior_failed_count=4, customer_prior_captured_count=2),
        _expectation(),
        diagnosis=_retryable_diagnosis(),
    )

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT
    assert "CUSTOMER_HISTORY_PRIOR_SUCCESS" in output.reason_codes


def test_history_can_never_override_a_stopping_rule():
    """A budget-exhausted payment stops regardless of how good the payer's
    history is. The stopping rule runs first and history cannot reach it."""
    context = _failed_context(customer_prior_failed_count=0, customer_prior_captured_count=50)
    context.fields = [f for f in context.fields if f.field != "prior_attempt_count"]
    context.fields.append(
        ProvenancedField(field="prior_attempt_count", value=5, band=ProvenanceBand.DERIVED, source="internal_count")
    )

    output = RecoveryEngine(retry_budget=2).evaluate(
        context, _expectation(), diagnosis=_retryable_diagnosis()
    )

    assert output.decision_type == DecisionType.RECOMMEND_STOP
    assert "RETRY_BUDGET_EXHAUSTED" in output.reason_codes


def test_history_can_never_rescue_a_terminal_failure_into_a_retry():
    terminal = Diagnosis(
        root_cause=RootCause.INSTRUMENT_BLOCKED_FOR_ONLINE,
        failure_class=FailureClass.TERMINAL,
        retry_advisable=False,
        confidence=0.95,
        rationale="instrument blocked for online use; retrying cannot succeed",
        model_version="test/diagnosis_v1",
    )

    output = RecoveryEngine().evaluate(
        _failed_context(customer_prior_failed_count=0, customer_prior_captured_count=99),
        _expectation(),
        diagnosis=terminal,
    )

    assert output.decision_type == DecisionType.RECOMMEND_STOP


def test_history_never_widens_authority_on_an_authorized_payment():
    """An authorized payment is decided before any history is consulted,
    so a payer's record cannot influence a capture recommendation."""
    context = ContextSnapshot(
        order_id="order_x",
        payment_attempt_id="pay_x",
        fields=[
            ProvenancedField(field="amount", value=50000, band=ProvenanceBand.RAW, source="razorpay_api_poll"),
            ProvenancedField(field="status", value="authorized", band=ProvenanceBand.RAW, source="razorpay_api_poll"),
            ProvenancedField(
                field="customer_prior_failed_count", value=99, band=ProvenanceBand.DERIVED, source="customer_history:email"
            ),
        ],
    )

    output = RecoveryEngine().evaluate(context, _expectation())

    assert output.decision_type == DecisionType.RECOMMEND_CAPTURE
    assert not any(code.startswith("CUSTOMER_HISTORY") for code in output.reason_codes)


# ---------------------------------------------------------------------------
# What reaches the persisted context
# ---------------------------------------------------------------------------


def test_the_context_builder_records_counts_and_a_fingerprint_but_no_address(db_conn):
    from context.builder import build_context_snapshot
    from repository.canonical_events import insert_canonical_event

    merchant_id = _merchant(db_conn)
    _attempt(db_conn, _order(db_conn, merchant_id), status="failed")

    order_id = _order(db_conn, merchant_id)
    payment_id = _attempt(db_conn, order_id, status="failed")

    payload = {"id": payment_id, "amount": 50000, "status": "failed", "email": CUSTOMER_EMAIL}
    event_id = insert_canonical_event(
        db_conn, merchant_id, "payment.attempt.failed", "razorpay_api_poll",
        "payment", payment_id, order_id, __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        payload,
    )
    event = {
        "id": event_id, "merchant_id": merchant_id, "event_type": "payment.attempt.failed",
        "entity_id": payment_id, "order_id": order_id, "payload": payload,
    }

    snapshot = build_context_snapshot(db_conn, event)
    by_name = {f.field: f.value for f in snapshot.fields}

    assert by_name["customer_prior_failed_count"] == 1
    assert by_name["customer_prior_captured_count"] == 0
    assert CUSTOMER_EMAIL not in str(snapshot.model_dump())
