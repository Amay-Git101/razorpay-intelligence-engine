"""RuleBasedEngine tests. Pure Python, no DB -- hand-built ContextSnapshot
and Expectation inputs, no reconciliation/context-builder/DB involved."""

from __future__ import annotations

from domain.contracts import ContextSnapshot, DecisionType, Expectation, ProvenanceBand, ProvenancedField
from intelligence.rule_based import RuleBasedEngine


def _payment_context(order_id: str = "order_x", payment_attempt_id: str = "pay_x", **field_overrides) -> ContextSnapshot:
    defaults = {"amount": 50000}
    defaults.update(field_overrides)
    fields = [
        ProvenancedField(field=name, value=value, band=ProvenanceBand.RAW, source="razorpay_api_poll")
        for name, value in defaults.items() if not name.startswith("_")
    ]
    if "_attempt_number" in field_overrides:
        fields.append(
            ProvenancedField(field="attempt_number", value=field_overrides["_attempt_number"], band=ProvenanceBand.DERIVED, source="internal_count")
        )
    return ContextSnapshot(order_id=order_id, payment_attempt_id=payment_attempt_id, fields=fields)


def _order_level_context(order_id: str = "order_x") -> ContextSnapshot:
    fields = [
        ProvenancedField(field="status", value="paid", band=ProvenanceBand.RAW, source="razorpay_api_poll")
    ]
    return ContextSnapshot(order_id=order_id, payment_attempt_id=None, fields=fields)


def _expectation(recovery_rate: float, sample_size: int, source: str = "rule_v1") -> Expectation:
    return Expectation(bucket_key="error_reason:payment_failed", expected_recovery_rate=recovery_rate, sample_size=sample_size, source=source)


def test_order_level_context_returns_no_action():
    engine = RuleBasedEngine()
    output = engine.evaluate(_order_level_context(), _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "ORDER_LEVEL_EVENT" in output.reason_codes
    assert output.confidence == 1.0


def test_gateway_failure_confidence_equals_expected_recovery_rate_exactly():
    engine = RuleBasedEngine()
    context = _payment_context(error_source="gateway", error_step="payment_authorization", error_reason="payment_failed", _attempt_number=2)
    output = engine.evaluate(context, _expectation(0.73, 25))

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT
    assert output.confidence == 0.73  # exact, no discount formula applied
    assert "GATEWAY_SIDE_FAILURE" in output.reason_codes
    assert "LOW_EVIDENCE" not in output.reason_codes


def test_zero_evidence_expectation_still_recommends_but_flags_low_evidence():
    engine = RuleBasedEngine()
    context = _payment_context(error_source="gateway", error_step="payment_authorization", error_reason="payment_failed", _attempt_number=1)
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT
    assert output.confidence == 0.5  # the default rate itself, not discounted further
    assert "LOW_EVIDENCE" in output.reason_codes


def test_customer_cancelled_returns_no_action():
    engine = RuleBasedEngine()
    context = _payment_context(error_source="customer", error_step="payment_authentication", error_reason="payment_cancelled", _attempt_number=1)
    output = engine.evaluate(context, _expectation(0.9, 50))  # even with a HIGH recovery rate available

    assert output.decision_type == DecisionType.NO_ACTION
    assert "CUSTOMER_CANCELLED" in output.reason_codes
    assert "NO_ACTION_REQUIRED" in output.reason_codes


def test_max_attempts_reached_returns_no_action():
    engine = RuleBasedEngine(max_retry_attempts=3)
    context = _payment_context(error_source="gateway", error_step="payment_authorization", error_reason="payment_failed", _attempt_number=3)
    output = engine.evaluate(context, _expectation(0.8, 10))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "MAX_ATTEMPTS_REACHED" in output.reason_codes


def test_below_attempt_ceiling_still_recommends():
    engine = RuleBasedEngine(max_retry_attempts=3)
    context = _payment_context(error_source="gateway", error_step="payment_authorization", error_reason="payment_failed", _attempt_number=2)
    output = engine.evaluate(context, _expectation(0.8, 10))

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT


def test_non_failure_payment_context_returns_no_action():
    # e.g. an authorized/captured attempt observed directly -- no
    # error_source/error_reason present at all.
    engine = RuleBasedEngine()
    context = _payment_context()  # only "amount", no error_* fields
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "NO_RECOMMENDATION_RULE_MATCHED" in output.reason_codes


def test_decision_output_model_version_is_rule_v1():
    engine = RuleBasedEngine()
    output = engine.evaluate(_order_level_context(), _expectation(0.5, 0, "rule_v1_default"))
    assert output.model_version == "rule_v1"


def test_recommend_retry_prompt_includes_expected_impact_amount():
    engine = RuleBasedEngine()
    context = _payment_context(error_source="gateway", error_step="payment_authorization", error_reason="payment_failed", amount=75000, _attempt_number=1)
    output = engine.evaluate(context, _expectation(0.6, 5))

    assert output.expected_impact.get("revenue_at_stake") == 75000


# ---------------------------------------------------------------------------
# RECOMMEND_CAPTURE rule -- closes the previous "hand-constructed
# Decision" gap in Scenario A/B (see intelligence/rule_based.py module
# docstring for the full confidence-semantics explanation).
# ---------------------------------------------------------------------------

def test_authorized_payment_recommends_capture_with_deterministic_confidence():
    engine = RuleBasedEngine()
    context = _payment_context(status="authorized", amount=10000)
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.RECOMMEND_CAPTURE
    assert output.confidence == 1.0
    assert output.reason_codes == ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"]
    assert output.expected_impact.get("revenue_at_stake") == 10000
    assert output.model_version == "rule_v1"


def test_authorized_payment_recommends_capture_regardless_of_amount():
    # No amount-based gating in the engine -- Policy alone decides
    # allow/block/approval. A large amount must still get the SAME
    # recommendation as a small one.
    engine = RuleBasedEngine()
    small = engine.evaluate(_payment_context(status="authorized", amount=10000), _expectation(0.5, 0, "rule_v1_default"))
    large = engine.evaluate(_payment_context(status="authorized", amount=500000), _expectation(0.5, 0, "rule_v1_default"))

    assert small.decision_type == large.decision_type == DecisionType.RECOMMEND_CAPTURE
    assert small.confidence == large.confidence == 1.0


def test_authorized_payment_recommends_capture_regardless_of_attempt_number():
    # attempt_number / max_retry_attempts is a retry-PROMPT stopping
    # rule -- it must not suppress a definite capture recommendation.
    engine = RuleBasedEngine(max_retry_attempts=3)
    context = _payment_context(status="authorized", amount=10000, _attempt_number=5)
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.RECOMMEND_CAPTURE


def test_captured_payment_does_not_recommend_capture():
    # The absence of error_source/error_reason is NOT sufficient proof
    # of capture-eligibility -- a captured context also has none, and
    # must not be mistaken for capture-ready.
    engine = RuleBasedEngine()
    context = _payment_context(status="captured", amount=10000)
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "NO_RECOMMENDATION_RULE_MATCHED" in output.reason_codes


def test_unrecognized_status_does_not_recommend_capture():
    engine = RuleBasedEngine()
    context = _payment_context(status="refunded", amount=10000)
    output = engine.evaluate(context, _expectation(0.5, 0, "rule_v1_default"))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "NO_RECOMMENDATION_RULE_MATCHED" in output.reason_codes


def test_failed_payment_with_status_still_follows_existing_failure_rule():
    # Adding status="failed" alongside a genuine failure context must not
    # change the existing, already-approved failure-handling behavior.
    engine = RuleBasedEngine()
    context = _payment_context(
        status="failed", error_source="gateway", error_step="payment_authorization",
        error_reason="payment_failed", _attempt_number=1,
    )
    output = engine.evaluate(context, _expectation(0.73, 25))

    assert output.decision_type == DecisionType.RECOMMEND_RETRY_PROMPT
    assert output.confidence == 0.73


def test_customer_cancelled_with_status_still_returns_no_action():
    engine = RuleBasedEngine()
    context = _payment_context(
        status="failed", error_source="customer", error_step="payment_authentication",
        error_reason="payment_cancelled", _attempt_number=1,
    )
    output = engine.evaluate(context, _expectation(0.9, 50))

    assert output.decision_type == DecisionType.NO_ACTION
    assert "CUSTOMER_CANCELLED" in output.reason_codes
