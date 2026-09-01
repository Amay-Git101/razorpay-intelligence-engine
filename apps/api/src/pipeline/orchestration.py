"""Shared reconciliation -> decision -> policy -> action -> verification
sequencing, extracted so both manual_run/ (the CLI) and the HTTP API can
call the exact same logic instead of either duplicating it.

This module contains no decision/policy/action/verification logic of
its own -- it only calls reconcile_order(), make_decision(),
propose_action(), and verify_action() in the same order Scenario A/B
already prove correct, and returns a structured, typed result instead
of printing. Presentation (CLI text vs. HTTP JSON) is entirely the
caller's responsibility.

EVENT PROCESSING RULE (unchanged from manual_run's original
implementation): only reconcile_order()'s own newly-returned event ids
are ever fed into make_decision() -- an order's full historical event
list is never reprocessed, since make_decision() itself does not
deduplicate decisions.

WRITE BOUNDARY: this module never imports or constructs
RazorpayWriteClient and never calls capture_payment() directly.
propose_action(..., write_client=None) remains the sole path that may
construct a write client.

TRANSACTION: each fully-processed event is committed durably before
moving to the next -- make_decision() and the repository write it
performs open no transaction of their own, so an explicit commit here
is required for the outcome to actually persist on a fresh connection.
A RazorpayAPIError from reconcile_order() or an UnresolvedEventError
both propagate to the caller; neither is swallowed here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from pydantic import BaseModel

from action.orchestrator import propose_action
from intelligence.orchestration import make_decision
from policy.orchestration import NotPolicyGated
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from verification.verifier import verify_action

_NO_ACTION_DECISION_TYPE = "NO_ACTION"
_VERIFYING_ACTION_STATUS = "VERIFYING"


class UnresolvedEventError(Exception):
    """Raised when reconcile_order() returns an event id that
    list_events_for_order() cannot resolve for the same order. Signals a
    genuine inconsistency -- callers must fail, never silently process a
    different event instead."""


class EventProcessingResult(BaseModel):
    """What happened for one newly-reconciled event. Fields are absent/
    None rather than fabricated when a stage never ran -- e.g.
    action_id/action_status stay None for a NO_ACTION decision."""

    event_id: str
    event_type: str
    decision_id: str
    decision_type: str
    action_id: str | None = None
    action_status: str | None = None
    action_skipped_reason: str | None = None
    verification_status: str | None = None


class PipelineRunResult(BaseModel):
    """Result of one run_reconciliation_pipeline() call."""

    order_id: str
    new_event_count: int
    events: list[EventProcessingResult]


def _process_event(conn: psycopg.Connection, merchant_id: str, event: dict[str, Any]) -> EventProcessingResult:
    decision_id = make_decision(conn, merchant_id, event)
    decision = get_decision(conn, decision_id)
    decision_type = decision["decision_type"]

    result = EventProcessingResult(
        event_id=str(event["id"]), event_type=event["event_type"],
        decision_id=str(decision_id), decision_type=decision_type,
    )

    if decision_type == _NO_ACTION_DECISION_TYPE:
        result.action_skipped_reason = "NO_ACTION"
        return result

    try:
        action = propose_action(conn, decision_id, write_client=None)
    except NotPolicyGated:
        result.action_skipped_reason = "decision_type is not policy-gated"
        return result

    result.action_id = str(action["id"])
    result.action_status = action["status"]

    if action["status"] == _VERIFYING_ACTION_STATUS:
        final_action = verify_action(conn, action["id"])
        result.verification_status = final_action["status"]

    return result


def run_reconciliation_pipeline(
    conn: psycopg.Connection, read_client: Any, merchant_id: str, order_id: str,
) -> PipelineRunResult:
    """Reconciles one order and processes exactly the newly-returned
    event ids through decision -> policy -> action -> verification.
    Caller is responsible for validating merchant existence first (this
    function does not) and for deciding how to present a raised
    RazorpayAPIError or UnresolvedEventError."""
    new_event_ids: list[UUID] = reconcile_order(conn, read_client, merchant_id, order_id)

    events: list[EventProcessingResult] = []
    if new_event_ids:
        events_by_id = {str(e["id"]): e for e in list_events_for_order(conn, order_id)}
        for event_id in new_event_ids:
            event = events_by_id.get(str(event_id))
            if event is None:
                raise UnresolvedEventError(f"reconciliation returned event {event_id}, which could not be resolved")
            events.append(_process_event(conn, merchant_id, event))
            conn.commit()  # durably persist this event's full outcome before moving on

    return PipelineRunResult(order_id=order_id, new_event_count=len(new_event_ids), events=events)
