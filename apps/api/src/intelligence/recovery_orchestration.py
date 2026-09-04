"""Assembles one recovery decision: context -> diagnosis -> intervention
-> persistence -> audit.

This is the failed-payment counterpart of orchestration.make_decision(),
which is left untouched and still serves the original capture path. The
difference is the middle step: this one consults the AI diagnosis layer and
feeds the result to RecoveryEngine, whereas make_decision() runs
RuleBasedEngine on the observed context alone.

TWO THINGS THIS MODULE IS CAREFUL ABOUT

  The diagnosis is recorded BEFORE the decision, as its own append-only
  audit entry (AI_DIAGNOSIS_RECORDED) keyed to the event. So the audit trail
  shows what the model said independently of what the system then did with
  it -- including the cases where the deterministic layer overrode the
  model's recommendation. An audit trail that only recorded the final
  decision would hide exactly the events a reviewer most wants to see.

  A failed diagnosis is recorded too, with its reason, and then passed to
  RecoveryEngine as `diagnosis=None`. It is never swallowed, and it never
  silently becomes a default classification.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from context.builder import build_context_snapshot
from diagnosis.diagnoser import DiagnosisModel, DiagnosisUnavailable
from diagnosis.signals import build_failure_signals
from domain.contracts import (
    ContextSnapshot,
    Diagnosis,
    Expectation,
    ProvenanceBand,
    ProvenancedField,
)
from intelligence.expectation import ZERO_EVIDENCE_SOURCE, compute_expectation
from intelligence.recovery_engine import (
    DEFAULT_CUSTOMER_FAILURE_ESCALATION_THRESHOLD,
    DEFAULT_MIN_DIAGNOSTIC_CONFIDENCE,
    DEFAULT_RETRY_BUDGET,
    RecoveryEngine,
)
from repository.audit import insert_audit_entry
from repository.decisions import insert_decision
from repository.merchants import get_merchant
from repository.payment_attempts import get_payment_attempt

from .orchestration import NO_ERROR_REASON_LABEL

DIAGNOSIS_SOURCE = "anthropic_diagnosis"


def _bucket_key_for_context(context: ContextSnapshot) -> str | None:
    for field in context.fields:
        if field.field == "error_reason":
            return f"error_reason:{field.value}"
    return None


def engine_for_merchant(conn: psycopg.Connection, merchant_id: str) -> RecoveryEngine:
    """Reads the per-merchant stopping rules out of merchants.automation_limits.

    That column existed from the first migration and was never read by
    anything until now. Absent or malformed values fall back to the
    conservative module defaults rather than to "no limit" -- a merchant with
    a misconfigured automation_limits gets fewer automated retries, never
    unlimited ones.
    """
    merchant = get_merchant(conn, merchant_id)
    limits: dict[str, Any] = (merchant or {}).get("automation_limits") or {}

    retry_budget = limits.get("max_recovery_attempts")
    if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or retry_budget < 0:
        retry_budget = DEFAULT_RETRY_BUDGET

    min_confidence = limits.get("min_diagnostic_confidence")
    if not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool) or not (0.0 <= min_confidence <= 1.0):
        min_confidence = DEFAULT_MIN_DIAGNOSTIC_CONFIDENCE

    # Same fail-closed treatment as the two above: a malformed value here
    # lowers the threshold to the conservative default, which sends MORE
    # payments to a human, never fewer.
    customer_threshold = limits.get("customer_failure_escalation_threshold")
    if not isinstance(customer_threshold, int) or isinstance(customer_threshold, bool) or customer_threshold < 1:
        customer_threshold = DEFAULT_CUSTOMER_FAILURE_ESCALATION_THRESHOLD

    return RecoveryEngine(
        retry_budget=retry_budget,
        min_diagnostic_confidence=float(min_confidence),
        customer_failure_escalation_threshold=customer_threshold,
    )


def _diagnose(
    conn: psycopg.Connection,
    diagnoser: DiagnosisModel | None,
    payment_attempt_id: str,
    prior_attempt_count: int,
    event_id: str,
) -> tuple[Diagnosis | None, str | None]:
    """Returns (diagnosis, error_reason). Exactly one is ever non-None."""
    if diagnoser is None:
        return None, "no_diagnoser_configured"

    payment_attempt = get_payment_attempt(conn, payment_attempt_id)
    if payment_attempt is None:
        return None, "payment_attempt_not_found"

    signals = build_failure_signals(payment_attempt, prior_attempt_count=prior_attempt_count)
    try:
        diagnosis = diagnoser.diagnose(signals)
    except DiagnosisUnavailable as exc:
        insert_audit_entry(
            conn,
            "AI_DIAGNOSIS_RECORDED",
            {"available": False, "reason": str(exc), "payment_attempt_id": payment_attempt_id},
            event_id=event_id,
        )
        return None, "diagnosis_unavailable"

    insert_audit_entry(
        conn,
        "AI_DIAGNOSIS_RECORDED",
        {
            "available": True,
            "payment_attempt_id": payment_attempt_id,
            "root_cause": diagnosis.root_cause.value,
            "failure_class": diagnosis.failure_class.value,
            "retry_advisable": diagnosis.retry_advisable,
            "confidence": diagnosis.confidence,
            "rationale": diagnosis.rationale,
            "model_version": diagnosis.model_version,
            # The exact evidence the model was shown. A reviewer can confirm
            # from the audit trail alone that no amount was ever passed to it.
            "signals_shown_to_model": signals.model_dump(),
        },
        event_id=event_id,
    )
    return diagnosis, None


def make_recovery_decision(
    conn: psycopg.Connection,
    merchant_id: str,
    event: dict[str, Any],
    diagnoser: DiagnosisModel | None,
    prior_attempt_count: int = 0,
) -> UUID:
    context = build_context_snapshot(conn, event)

    # prior_attempt_count is what the retry-budget stopping rule reads. It
    # is DERIVED (counted from our own rows), not RAW, and is recorded on the
    # context so the persisted decision shows the number the rule actually
    # used rather than requiring it to be recomputed later.
    context.fields.append(
        ProvenancedField(
            field="prior_attempt_count",
            value=prior_attempt_count,
            band=ProvenanceBand.DERIVED,
            source="internal_count",
        )
    )

    diagnosis: Diagnosis | None = None
    diagnosis_error: str | None = None
    status_field = next((f.value for f in context.fields if f.field == "status"), None)

    # Only failures are diagnosed. An authorized-not-captured payment has no
    # failure to explain, so calling the model would spend money to classify
    # a non-event.
    if status_field == "failed" and context.payment_attempt_id is not None:
        diagnosis, diagnosis_error = _diagnose(
            conn, diagnoser, context.payment_attempt_id, prior_attempt_count, str(event["id"])
        )
        if diagnosis is not None:
            context.fields.extend(diagnosis.to_provenanced_fields(DIAGNOSIS_SOURCE))

    bucket_key = _bucket_key_for_context(context)
    if bucket_key is None:
        expectation = Expectation(
            bucket_key=NO_ERROR_REASON_LABEL,
            expected_recovery_rate=0.5,
            sample_size=0,
            source=ZERO_EVIDENCE_SOURCE,
        )
    else:
        expectation = compute_expectation(conn, merchant_id, bucket_key)

    output = engine_for_merchant(conn, merchant_id).evaluate(
        context, expectation, diagnosis=diagnosis, diagnosis_error=diagnosis_error
    )

    decision_id = insert_decision(
        conn,
        merchant_id,
        context.order_id,
        context.payment_attempt_id,
        str(event["id"]),
        context.model_dump(mode="json"),
        expectation.model_dump(mode="json"),
        output.decision_type.value,
        output.confidence,
        output.reason_codes,
        output.expected_impact,
        output.model_version,
    )

    insert_audit_entry(
        conn,
        "DECISION_CREATED",
        {"decision_type": output.decision_type.value, "reason_codes": output.reason_codes},
        event_id=str(event["id"]),
        decision_id=str(decision_id),
    )

    return decision_id
