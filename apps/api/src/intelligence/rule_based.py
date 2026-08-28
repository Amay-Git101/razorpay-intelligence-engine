"""RuleBasedEngine: the v1 IntelligenceEngine implementation. Deterministic,
no ML.

Locked decisions from Phase 3 gate review:
  - Confidence for a recovery recommendation is EXACTLY
    expectation.expected_recovery_rate. No discount formula, floor, or
    sample-size-weighting threshold is applied. Evidence strength is
    communicated via the LOW_EVIDENCE reason code (when
    expectation.sample_size == 0) and via the persisted Expectation's
    own source/sample_size fields -- never by distorting confidence.
  - Customer-side `payment_cancelled` failures never get a retry prompt:
    NO_ACTION with reason codes CUSTOMER_CANCELLED, NO_ACTION_REQUIRED.
  - NO_ACTION decisions carry confidence=1.0 (these are rule-certain
    outcomes -- "we are sure there's nothing to recommend here" -- not a
    probabilistic recovery estimate, so tying them to
    expected_recovery_rate would misrepresent what the number means).

RECOMMEND_CAPTURE rule (closes the previous "hand-constructed Decision"
gap in Scenario A/B): fires only when the context's own `status` field
is exactly "authorized" -- never inferred merely from the absence of an
error_source/error_reason, since a captured or otherwise-unusual context
also has no error fields and must NOT be mistaken for capture-eligible.

CONFIDENCE SEMANTICS FOR THIS RULE: confidence=1.0 here means the
deterministic rule is certain the OBSERVED CONTEXT satisfies the rule
(status is unambiguously "authorized") -- it is NOT a claim that the
capture call itself has a 100% probability of succeeding. Whether the
capture actually succeeds is entirely Action's and Verification's
concern, established independently afterward; this number never feeds
into or substitutes for that.

No amount-based gating happens here, deliberately: RuleBasedEngine
recommends capture for an authorized payment regardless of amount.
Auto-allow / approval-required / hard-block is Policy's exclusive
responsibility -- see policy/rules.py.
"""

from __future__ import annotations

from typing import Any

from domain.contracts import ContextSnapshot, DecisionOutput, DecisionType, Expectation

MODEL_VERSION = "rule_v1"

# Placeholder default, not yet wired to per-merchant automation_limits --
# that belongs to the Policy gate, next. Not a confidence-discount
# formula; this is a stopping-rule-adjacent ceiling, which was part of
# the originally approved gate scope.
DEFAULT_MAX_RETRY_ATTEMPTS = 3


def _field_value(context: ContextSnapshot, name: str) -> Any | None:
    for field in context.fields:
        if field.field == name:
            return field.value
    return None


class RuleBasedEngine:
    def __init__(self, max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS):
        self.max_retry_attempts = max_retry_attempts

    def evaluate(self, context: ContextSnapshot, expectation: Expectation) -> DecisionOutput:
        if context.payment_attempt_id is None:
            # Order-level event (order.created / order.paid) -- nothing
            # to recommend on an observation that isn't about a specific
            # payment attempt.
            return self._no_action(["ORDER_LEVEL_EVENT", "NO_ACTION_REQUIRED"])

        error_source = _field_value(context, "error_source")
        error_reason = _field_value(context, "error_reason")
        status = _field_value(context, "status")
        attempt_number = _field_value(context, "attempt_number")

        # Authorized-and-not-yet-captured -- eligible for a capture
        # recommendation regardless of attempt_number (that counter is a
        # retry-prompt-specific stopping rule, not applicable to a
        # definite, already-successful authorization) and regardless of
        # amount (Policy's job, not this engine's). Explicitly requires
        # status == "authorized" -- NOT merely "no error_source/
        # error_reason present", since a captured context also has none.
        if status == "authorized":
            amount = _field_value(context, "amount")
            return DecisionOutput(
                decision_type=DecisionType.RECOMMEND_CAPTURE,
                confidence=1.0,
                reason_codes=["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"],
                expected_impact={"revenue_at_stake": amount} if amount is not None else {},
                model_version=MODEL_VERSION,
            )

        if error_source == "customer" and error_reason == "payment_cancelled":
            return self._no_action(["CUSTOMER_CANCELLED", "NO_ACTION_REQUIRED"])

        if attempt_number is not None and attempt_number >= self.max_retry_attempts:
            return self._no_action(["MAX_ATTEMPTS_REACHED"])

        if error_source == "gateway":
            reason_codes = ["GATEWAY_SIDE_FAILURE", "RECOVERABLE_FAILURE_PATTERN"]
            if expectation.sample_size == 0:
                reason_codes.append("LOW_EVIDENCE")
            amount = _field_value(context, "amount")
            return DecisionOutput(
                decision_type=DecisionType.RECOMMEND_RETRY_PROMPT,
                confidence=expectation.expected_recovery_rate,
                reason_codes=reason_codes,
                expected_impact={"revenue_at_stake": amount} if amount is not None else {},
                model_version=MODEL_VERSION,
            )

        # Any other payment-attempt context this gate doesn't have a rule
        # for -- e.g. a captured attempt (already done, nothing to
        # recommend), an unrecognized status value, or an unrecognized
        # error_source. Explicitly NO_ACTION rather than guessing.
        return self._no_action(["NO_RECOMMENDATION_RULE_MATCHED"])

    def _no_action(self, reason_codes: list[str]) -> DecisionOutput:
        return DecisionOutput(
            decision_type=DecisionType.NO_ACTION,
            confidence=1.0,
            reason_codes=reason_codes,
            expected_impact={},
            model_version=MODEL_VERSION,
        )
