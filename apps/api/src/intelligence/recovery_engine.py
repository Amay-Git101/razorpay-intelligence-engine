"""Deterministic intervention selection.

This module answers the Buildathon track's second requirement -- "determines
the right intervention" -- and it answers it WITHOUT a language model. The
model's job (diagnosis/) is to classify why a payment failed. This module's
job is to decide what may be done about it. The split is the whole safety
argument:

    AI decides WHAT HAPPENED.  Deterministic code decides WHAT WE DO.
    Policy decides WHETHER MONEY MOVES.

Three properties are worth checking against the code below, because they are
what a skeptical reviewer should attack first:

  1. THE MODEL CANNOT OVERRIDE A STOPPING RULE. The retry-budget check runs
     BEFORE the diagnosis is consulted at all. A payment that has exhausted
     its budget stops, no matter how confidently the model recommends a
     retry. Rule order here is a security property, not a style choice.

  2. AN ABSENT OR UNCERTAIN MODEL PRODUCES MORE HUMAN REVIEW, NOT MORE
     AUTOMATION. No diagnosis, low confidence, an AMBIGUOUS class, or a
     self-contradictory diagnosis all route to RECOMMEND_ESCALATION. There
     is no path on which a degraded model widens what the system may do.

  3. NO AMOUNT IS READ HERE FOR GATING. The amount is copied into
     expected_impact so Policy can gate on it downstream, and is never
     compared against a threshold in this file. Intervention selection and
     money-limit enforcement are separate stages on purpose.

RuleBasedEngine (rule_based.py) is left exactly as it was and is still the
engine used for the authorized-payment capture path. This engine is the
failed-payment recovery path. They produce the same DecisionOutput contract,
so everything downstream is unchanged.
"""

from __future__ import annotations

from typing import Any

from domain.contracts import (
    ContextSnapshot,
    DecisionOutput,
    DecisionType,
    Diagnosis,
    Expectation,
    FailureClass,
)

MODEL_VERSION = "recovery_v1"

# Defaults, overridden per merchant from merchants.automation_limits.
# Until this gate, automation_limits was an unused column.
DEFAULT_RETRY_BUDGET = 2
DEFAULT_MIN_DIAGNOSTIC_CONFIDENCE = 0.70
# How many prior failed payments by the same payer -- with no prior
# successful one -- send a fresh failure to a human instead of to an
# automated retry prompt. Deliberately a count of observed outcomes, not a
# score or a standing judgement about the payer.
DEFAULT_CUSTOMER_FAILURE_ESCALATION_THRESHOLD = 3


def _field_value(context: ContextSnapshot, name: str) -> Any | None:
    for field in context.fields:
        if field.field == name:
            return field.value
    return None


class RecoveryEngine:
    """Maps (observed context, optional diagnosis) -> a recommended
    intervention. Pure: no database, no network, no clock."""

    def __init__(
        self,
        retry_budget: int = DEFAULT_RETRY_BUDGET,
        min_diagnostic_confidence: float = DEFAULT_MIN_DIAGNOSTIC_CONFIDENCE,
        customer_failure_escalation_threshold: int = DEFAULT_CUSTOMER_FAILURE_ESCALATION_THRESHOLD,
    ):
        self.retry_budget = retry_budget
        self.min_diagnostic_confidence = min_diagnostic_confidence
        self.customer_failure_escalation_threshold = customer_failure_escalation_threshold

    def evaluate(
        self,
        context: ContextSnapshot,
        expectation: Expectation,
        diagnosis: Diagnosis | None = None,
        diagnosis_error: str | None = None,
    ) -> DecisionOutput:
        amount = _field_value(context, "amount")
        impact = {"revenue_at_stake": amount} if amount is not None else {}
        status = _field_value(context, "status")

        if context.payment_attempt_id is None:
            return self._decide(DecisionType.NO_ACTION, 1.0, ["ORDER_LEVEL_EVENT", "NO_ACTION_REQUIRED"], {}, None)

        # An authorized payment is not a failure and needs no diagnosis --
        # nothing went wrong, the money simply has not been taken yet.
        if status == "authorized":
            return self._decide(
                DecisionType.RECOMMEND_CAPTURE, 1.0, ["AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE"], impact, None
            )

        if status != "failed":
            return self._decide(DecisionType.NO_ACTION, 1.0, ["NO_RECOMMENDATION_RULE_MATCHED"], {}, None)

        # ---- Stopping rule. Deliberately evaluated BEFORE the diagnosis. ----
        # Property 1 in the module docstring: nothing the model returns can
        # reach this branch and reverse it, because the model's output is not
        # read until after it.
        prior_attempts = _field_value(context, "prior_attempt_count") or 0
        if prior_attempts >= self.retry_budget:
            return self._decide(
                DecisionType.RECOMMEND_STOP,
                1.0,
                ["RETRY_BUDGET_EXHAUSTED", f"PRIOR_ATTEMPTS:{prior_attempts}", f"BUDGET:{self.retry_budget}"],
                impact,
                diagnosis,
            )

        # ---- From here the diagnosis is consulted. Every degraded case
        # ---- below routes to a human (property 2).
        if diagnosis is None:
            return self._decide(
                DecisionType.RECOMMEND_ESCALATION,
                1.0,
                ["DIAGNOSIS_UNAVAILABLE", f"REASON:{diagnosis_error or 'not_attempted'}"],
                impact,
                None,
            )

        if diagnosis.confidence < self.min_diagnostic_confidence:
            return self._decide(
                DecisionType.RECOMMEND_ESCALATION,
                1.0,
                [
                    "LOW_DIAGNOSTIC_CONFIDENCE",
                    f"CONFIDENCE:{diagnosis.confidence:.2f}",
                    f"THRESHOLD:{self.min_diagnostic_confidence:.2f}",
                ],
                impact,
                diagnosis,
            )

        if diagnosis.failure_class == FailureClass.AMBIGUOUS:
            return self._decide(
                DecisionType.RECOMMEND_ESCALATION,
                1.0,
                ["AMBIGUOUS_FAILURE_CLASS", f"ROOT_CAUSE:{diagnosis.root_cause.value}"],
                impact,
                diagnosis,
            )

        # ---- Second stopping rule: the failure is terminal, so retrying the
        # ---- same instrument is waste. Stopping IS the correct recovery
        # ---- outcome here, not a failure to recover.
        if diagnosis.failure_class == FailureClass.TERMINAL:
            return self._decide(
                DecisionType.RECOMMEND_STOP,
                1.0,
                ["TERMINAL_FAILURE_NOT_RECOVERABLE", f"ROOT_CAUSE:{diagnosis.root_cause.value}"],
                impact,
                diagnosis,
            )

        # TRANSIENT from here.
        if not diagnosis.retry_advisable:
            # The model called the failure recoverable but advised against a
            # retry. That is a genuine internal contradiction, not a case to
            # resolve by picking whichever half we prefer -- a human decides.
            return self._decide(
                DecisionType.RECOMMEND_ESCALATION,
                1.0,
                ["TRANSIENT_BUT_RETRY_NOT_ADVISED", f"ROOT_CAUSE:{diagnosis.root_cause.value}"],
                impact,
                diagnosis,
            )

        # ---- Customer payment history. Reached ONLY here, on the single
        # ---- branch that would otherwise produce an automated retry.
        #
        # Position is the safety argument. Every stopping rule and every
        # escalation above has already been decided, so this rule cannot
        # reverse a STOP, cannot rescue an escalation into automation, and
        # cannot fire at all unless the system was about to act on its own.
        # The only transition it can cause is automation -> human review.
        # A customer with no observed history, or none recorded, leaves the
        # decision exactly as it was before this rule existed.
        prior_customer_failures = _field_value(context, "customer_prior_failed_count")
        prior_customer_captures = _field_value(context, "customer_prior_captured_count")

        if (
            prior_customer_failures is not None
            and prior_customer_failures >= self.customer_failure_escalation_threshold
            and (prior_customer_captures or 0) == 0
        ):
            return self._decide(
                DecisionType.RECOMMEND_ESCALATION,
                1.0,
                [
                    "CUSTOMER_HISTORY_REPEATED_FAILURES",
                    f"PRIOR_CUSTOMER_FAILURES:{prior_customer_failures}",
                    f"PRIOR_CUSTOMER_CAPTURES:{prior_customer_captures or 0}",
                    f"THRESHOLD:{self.customer_failure_escalation_threshold}",
                ],
                impact,
                diagnosis,
            )

        reason_codes = ["TRANSIENT_FAILURE_RETRY_ADVISED", f"ROOT_CAUSE:{diagnosis.root_cause.value}"]
        if expectation.sample_size == 0:
            reason_codes.append("LOW_EVIDENCE")
        # Supporting evidence only. A prior successful payment is recorded on
        # the decision because it is part of why this retry looks reasonable,
        # but it widens nothing: the decision here is already
        # RECOMMEND_RETRY_PROMPT, and a customer with a strong history cannot
        # obtain any authority a customer with no history would not get.
        if prior_customer_captures:
            reason_codes.append("CUSTOMER_HISTORY_PRIOR_SUCCESS")
            reason_codes.append(f"PRIOR_CUSTOMER_CAPTURES:{prior_customer_captures}")
        # Confidence on a recovery recommendation keeps RuleBasedEngine's
        # semantics exactly: it is the expected probability of recovery from
        # the calibrated baseline, NOT the model's confidence in its own
        # classification. Those are different quantities and are never
        # conflated -- the model's confidence is recorded separately on the
        # AI_OUTPUT context fields.
        return self._decide(
            DecisionType.RECOMMEND_RETRY_PROMPT,
            expectation.expected_recovery_rate,
            reason_codes,
            impact,
            diagnosis,
        )

    def _decide(
        self,
        decision_type: DecisionType,
        confidence: float,
        reason_codes: list[str],
        expected_impact: dict[str, Any],
        diagnosis: Diagnosis | None,
    ) -> DecisionOutput:
        # model_version records BOTH halves when a model informed the
        # decision, so a persisted decision can never be mistaken for a
        # purely deterministic one, or vice versa.
        version = MODEL_VERSION if diagnosis is None else f"{MODEL_VERSION}+{diagnosis.model_version}"
        return DecisionOutput(
            decision_type=decision_type,
            confidence=confidence,
            reason_codes=reason_codes,
            expected_impact=expected_impact,
            model_version=version,
        )
