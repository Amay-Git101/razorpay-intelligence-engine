"""Intervention selection: the deterministic layer between the model and the
money.

The tests are grouped by the three properties RecoveryEngine's docstring
claims, because those claims are the reason it is safe to let a language
model near this pipeline at all. Each one is checked adversarially -- with a
model output deliberately constructed to try to get the wrong answer.
"""

from __future__ import annotations

import pytest

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


def _context(status: str = "failed", amount: int = 250_000, prior_attempts: int = 0) -> ContextSnapshot:
    return ContextSnapshot(
        order_id="order_TEST",
        payment_attempt_id="pay_TEST",
        fields=[
            ProvenancedField(field="amount", value=amount, band=ProvenanceBand.RAW, source="test"),
            ProvenancedField(field="status", value=status, band=ProvenanceBand.RAW, source="test"),
            ProvenancedField(
                field="prior_attempt_count", value=prior_attempts, band=ProvenanceBand.DERIVED, source="test"
            ),
        ],
    )


def _expectation(rate: float = 0.42, sample_size: int = 12) -> Expectation:
    return Expectation(
        bucket_key="error_reason:insufficient_funds",
        expected_recovery_rate=rate,
        sample_size=sample_size,
        source="test",
    )


def _diagnosis(
    failure_class: FailureClass = FailureClass.TRANSIENT,
    retry_advisable: bool = True,
    confidence: float = 0.9,
    root_cause: RootCause = RootCause.INSUFFICIENT_FUNDS,
) -> Diagnosis:
    return Diagnosis(
        root_cause=root_cause,
        failure_class=failure_class,
        retry_advisable=retry_advisable,
        confidence=confidence,
        rationale="test rationale",
        model_version="test-model/v1",
    )


# ---------------------------------------------------------------------------
# Property 1: the model cannot override a stopping rule
# ---------------------------------------------------------------------------

def test_retry_budget_stops_the_payment_even_when_the_model_is_maximally_confident():
    """The adversarial case. A model that is certain the failure is transient
    and certain a retry is warranted must still not get one once the budget
    is spent. If this ever fails, the model has been given authority over a
    stopping rule."""
    engine = RecoveryEngine(retry_budget=2)
    output = engine.evaluate(
        _context(prior_attempts=2),
        _expectation(),
        diagnosis=_diagnosis(failure_class=FailureClass.TRANSIENT, retry_advisable=True, confidence=1.0),
    )
    assert output.decision_type is DecisionType.RECOMMEND_STOP
    assert "RETRY_BUDGET_EXHAUSTED" in output.reason_codes


def test_retry_budget_is_checked_before_the_diagnosis_is_read_at_all():
    """Stronger form: with the budget exhausted, the engine must reach STOP
    even when no diagnosis exists. Proves the budget branch precedes the
    diagnosis branch rather than merely outranking it."""
    engine = RecoveryEngine(retry_budget=1)
    output = engine.evaluate(_context(prior_attempts=5), _expectation(), diagnosis=None)
    assert output.decision_type is DecisionType.RECOMMEND_STOP
    assert "RETRY_BUDGET_EXHAUSTED" in output.reason_codes
    assert "DIAGNOSIS_UNAVAILABLE" not in output.reason_codes


def test_budget_of_zero_stops_everything_immediately():
    engine = RecoveryEngine(retry_budget=0)
    output = engine.evaluate(_context(prior_attempts=0), _expectation(), diagnosis=_diagnosis())
    assert output.decision_type is DecisionType.RECOMMEND_STOP


# ---------------------------------------------------------------------------
# Property 2: a degraded model produces more human review, never more automation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "diagnosis, expected_reason",
    [
        (None, "DIAGNOSIS_UNAVAILABLE"),
        (_diagnosis(confidence=0.10), "LOW_DIAGNOSTIC_CONFIDENCE"),
        (_diagnosis(confidence=0.69), "LOW_DIAGNOSTIC_CONFIDENCE"),
        (_diagnosis(failure_class=FailureClass.AMBIGUOUS), "AMBIGUOUS_FAILURE_CLASS"),
        (_diagnosis(failure_class=FailureClass.TRANSIENT, retry_advisable=False), "TRANSIENT_BUT_RETRY_NOT_ADVISED"),
    ],
)
def test_every_degraded_diagnosis_routes_to_human_escalation(diagnosis, expected_reason):
    engine = RecoveryEngine(retry_budget=3, min_diagnostic_confidence=0.70)
    output = engine.evaluate(_context(), _expectation(), diagnosis=diagnosis, diagnosis_error="model_down")
    assert output.decision_type is DecisionType.RECOMMEND_ESCALATION
    assert expected_reason in output.reason_codes


def test_no_degraded_path_ever_produces_an_automated_money_moving_recommendation():
    """Exhaustive sweep: across every combination of failure class, retry
    advice and confidence, a failed payment must never yield
    RECOMMEND_CAPTURE. Capture is the only money-moving recommendation, and
    nothing the model says about a FAILED payment may reach it."""
    engine = RecoveryEngine(retry_budget=3)
    for failure_class in FailureClass:
        for retry_advisable in (True, False):
            for confidence in (0.0, 0.5, 0.7, 0.95, 1.0):
                output = engine.evaluate(
                    _context(),
                    _expectation(),
                    diagnosis=_diagnosis(
                        failure_class=failure_class, retry_advisable=retry_advisable, confidence=confidence
                    ),
                )
                assert output.decision_type is not DecisionType.RECOMMEND_CAPTURE


# ---------------------------------------------------------------------------
# Property 3: no amount gating happens here
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount", [1, 50_000, 500_000, 1_000_001, 25_000_000, 999_999_999])
def test_the_decision_is_identical_regardless_of_amount(amount):
    """Amount must not change intervention selection at all -- that is
    Policy's exclusive job. The amount is only ever copied into
    expected_impact for Policy to read downstream."""
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(_context(amount=amount), _expectation(), diagnosis=_diagnosis())
    assert output.decision_type is DecisionType.RECOMMEND_RETRY_PROMPT
    assert output.expected_impact["revenue_at_stake"] == amount


# ---------------------------------------------------------------------------
# The intended mappings
# ---------------------------------------------------------------------------

def test_terminal_failure_stops_rather_than_escalating():
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(
        _context(),
        _expectation(),
        diagnosis=_diagnosis(failure_class=FailureClass.TERMINAL, retry_advisable=False, root_cause=RootCause.INSTRUMENT_BLOCKED_FOR_ONLINE),
    )
    assert output.decision_type is DecisionType.RECOMMEND_STOP
    assert "TERMINAL_FAILURE_NOT_RECOVERABLE" in output.reason_codes
    assert "ROOT_CAUSE:INSTRUMENT_BLOCKED_FOR_ONLINE" in output.reason_codes


def test_transient_and_retry_advised_recommends_a_retry_prompt():
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(_context(), _expectation(rate=0.42), diagnosis=_diagnosis())
    assert output.decision_type is DecisionType.RECOMMEND_RETRY_PROMPT
    assert "TRANSIENT_FAILURE_RETRY_ADVISED" in output.reason_codes


def test_recovery_confidence_is_the_calibrated_recovery_rate_not_the_models_confidence():
    """These are different quantities and conflating them would misreport
    both. The model was 0.99 confident in its CLASSIFICATION; the expected
    probability of RECOVERY is 0.42."""
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(
        _context(), _expectation(rate=0.42), diagnosis=_diagnosis(confidence=0.99)
    )
    assert output.confidence == pytest.approx(0.42)


def test_low_evidence_is_flagged_rather_than_discounted_into_the_confidence():
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(
        _context(), _expectation(rate=0.5, sample_size=0), diagnosis=_diagnosis()
    )
    assert "LOW_EVIDENCE" in output.reason_codes
    assert output.confidence == pytest.approx(0.5)


def test_authorized_payment_takes_the_capture_path_without_consulting_a_model():
    engine = RecoveryEngine(retry_budget=0)  # budget irrelevant: not a failure
    output = engine.evaluate(_context(status="authorized"), _expectation(), diagnosis=None)
    assert output.decision_type is DecisionType.RECOMMEND_CAPTURE
    assert "AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE" in output.reason_codes


def test_order_level_event_yields_no_action():
    engine = RecoveryEngine()
    context = ContextSnapshot(order_id="order_TEST", payment_attempt_id=None, fields=[])
    assert engine.evaluate(context, _expectation()).decision_type is DecisionType.NO_ACTION


def test_captured_payment_yields_no_action():
    engine = RecoveryEngine()
    output = engine.evaluate(_context(status="captured"), _expectation())
    assert output.decision_type is DecisionType.NO_ACTION


# ---------------------------------------------------------------------------
# Provenance of the decision itself
# ---------------------------------------------------------------------------

def test_model_version_records_both_halves_when_a_model_informed_the_decision():
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(_context(), _expectation(), diagnosis=_diagnosis())
    assert output.model_version == "recovery_v1+test-model/v1"


def test_model_version_records_the_deterministic_half_alone_when_no_model_was_used():
    engine = RecoveryEngine(retry_budget=3)
    output = engine.evaluate(_context(status="authorized"), _expectation(), diagnosis=None)
    assert output.model_version == "recovery_v1"
