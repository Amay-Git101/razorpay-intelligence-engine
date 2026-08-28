"""Chains context -> expectation -> engine -> persistence -> audit for a
single canonical_events row. First piece of code that ties all four
already-built layers together end-to-end. No Policy/Action/Verification
here -- a Decision is created and audited; nothing acts on it yet.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from context.builder import build_context_snapshot
from domain.contracts import ContextSnapshot, Expectation
from intelligence.expectation import ZERO_EVIDENCE_SOURCE, compute_expectation
from intelligence.rule_based import RuleBasedEngine
from repository.audit import insert_audit_entry
from repository.decisions import insert_decision

# Used only as the Expectation object's own bucket_key label when there
# is no error_reason to bucket on -- NEVER used as a key to read or write
# expectation_baselines. Locked decision: contexts without an
# error_reason must not create/read a fabricated "unknown" calibrated
# bucket.
NO_ERROR_REASON_LABEL = "no_error_reason"


def _bucket_key_for_context(context: ContextSnapshot) -> str | None:
    for field in context.fields:
        if field.field == "error_reason":
            return f"error_reason:{field.value}"
    return None


def make_decision(conn: psycopg.Connection, merchant_id: str, event: dict[str, Any]) -> UUID:
    context = build_context_snapshot(conn, event)

    bucket_key = _bucket_key_for_context(context)
    if bucket_key is None:
        # No error_reason on this context (order-level event, or a
        # payment-attempt context with no failure to bucket on) -- use
        # the explicit zero-evidence default directly. Do NOT call
        # compute_expectation with a fabricated bucket key: that would
        # read (and risk later writes to) a nonsensical
        # expectation_baselines row.
        expectation = Expectation(
            bucket_key=NO_ERROR_REASON_LABEL,
            expected_recovery_rate=0.5,
            sample_size=0,
            source=ZERO_EVIDENCE_SOURCE,
        )
    else:
        expectation = compute_expectation(conn, merchant_id, bucket_key)

    output = RuleBasedEngine().evaluate(context, expectation)

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
