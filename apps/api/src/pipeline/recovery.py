"""The bounded recovery workflow, run across a batch.

    detect -> (per item) diagnose -> select intervention -> policy -> authorize
           -> execute -> verify -> audit -> ledger

This is the top-level entry point for the Buildathon track's third
requirement, "executes a bounded recovery workflow", and it is what the
demo runs.

WHAT MAKES IT BOUNDED -- each of these is enforced somewhere else and merely
relied on here, which is the point: this module is a sequencer, not a place
where authority is decided.

  * Policy gates every intervention, including the ones that cannot move
    money (policy/orchestration.py).
  * The database refuses to let an action reach AUTHORIZED or EXECUTING
    unless its own persisted policy_evaluation.allowed is true
    (0003_action_authorization_guard.sql).
  * Capture is attempted at most once, ever, per action, with no retry loop
    anywhere (action/orchestrator.py).
  * Idempotency is keyed on the real-world operation, so re-running a batch
    cannot execute the same capture twice (domain/contracts.py).
  * The retry-budget and terminal-failure stopping rules are evaluated
    before the model's recommendation is read (intelligence/recovery_engine.py).

PER-ITEM ISOLATION: each item is committed on its own, and a failure on one
item is recorded against that item and does not abort the batch. A single
malformed payment must not prevent the other 39 from being recovered. Errors
are recorded per item, never swallowed silently -- every item in the result
says what happened to it.

WRITE BOUNDARY (unchanged): this module never imports RazorpayWriteClient
and never calls capture_payment(). propose_action(..., write_client=None)
remains the sole path that may construct a write client.
"""

from __future__ import annotations

from typing import Any

import psycopg
from pydantic import BaseModel

from action.orchestrator import propose_action
from diagnosis.diagnoser import DiagnosisModel
from intelligence.recovery_orchestration import make_recovery_decision
from observability.batch_ledger import BatchLedger, build_batch_ledger
from policy.orchestration import NotPolicyGated
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from risk.batch_progress import claim_unprocessed_items, record_item_decision
from risk.detection import detect_revenue_at_risk
from verification.verifier import verify_action

_NO_ACTION_DECISION_TYPE = "NO_ACTION"
_VERIFYING_ACTION_STATUS = "VERIFYING"


class RecoveryItemResult(BaseModel):
    """What happened to one at-risk payment. A stage that never ran stays
    None rather than being filled with a plausible-looking value."""

    order_id: str
    payment_attempt_id: str
    amount_at_risk: int
    decision_id: str | None = None
    decision_type: str | None = None
    action_id: str | None = None
    action_status: str | None = None
    verification_status: str | None = None
    skipped_reason: str | None = None
    error: str | None = None


class RecoveryBatchResult(BaseModel):
    batch_id: str
    merchant_id: str
    source: str
    detected_count: int
    revenue_at_risk: int
    processed_count: int
    items: list[RecoveryItemResult]
    ledger: BatchLedger | None = None


def _latest_event_for_attempt(conn: psycopg.Connection, order_id: str, payment_attempt_id: str) -> dict[str, Any] | None:
    """The canonical event this decision will be anchored to.

    Returns None rather than inventing an event if the attempt has none.
    decisions.event_id is NOT NULL and references canonical_events, so a
    decision genuinely cannot exist without an observation behind it -- that
    constraint is load-bearing for the audit trail's meaning and is not
    worked around here.
    """
    events = [e for e in list_events_for_order(conn, order_id) if e["entity_id"] == payment_attempt_id]
    return events[-1] if events else None


def _process_item(
    conn: psycopg.Connection,
    merchant_id: str,
    batch_id: str,
    item: dict[str, Any],
    diagnoser: DiagnosisModel | None,
) -> RecoveryItemResult:
    result = RecoveryItemResult(
        order_id=item["order_id"],
        payment_attempt_id=item["payment_attempt_id"],
        amount_at_risk=item["amount_at_risk"],
    )

    event = _latest_event_for_attempt(conn, item["order_id"], item["payment_attempt_id"])
    if event is None:
        result.skipped_reason = "no canonical event for this payment attempt"
        return result

    # prior_attempt_count was computed at detection time from the same rows
    # the stopping rule cares about; recomputing it here could yield a
    # different number than the one the batch was detected against.
    prior_attempt_count = item.get("prior_attempt_count", 0)

    decision_id = make_recovery_decision(
        conn, merchant_id, event, diagnoser, prior_attempt_count=prior_attempt_count
    )
    record_item_decision(conn, batch_id, item["payment_attempt_id"], decision_id)

    decision = get_decision(conn, decision_id)
    result.decision_id = str(decision_id)
    result.decision_type = decision["decision_type"]

    if decision["decision_type"] == _NO_ACTION_DECISION_TYPE:
        result.skipped_reason = "NO_ACTION"
        return result

    try:
        action = propose_action(conn, decision_id, write_client=None)
    except NotPolicyGated as exc:
        result.skipped_reason = f"not policy-gated: {exc}"
        return result

    result.action_id = str(action["id"])
    result.action_status = action["status"]

    if action["status"] == _VERIFYING_ACTION_STATUS:
        final_action = verify_action(conn, action["id"])
        result.verification_status = final_action["status"]

    return result


def run_recovery_batch(
    conn: psycopg.Connection,
    merchant_id: str,
    source: str,
    diagnoser: DiagnosisModel | None = None,
    limit: int | None = None,
) -> RecoveryBatchResult:
    """Detects at-risk revenue for one merchant and runs the full recovery
    workflow over every detected item.

    `source` is passed straight through to detection, which refuses anything
    but 'razorpay_test_mode' or 'synthetic'. `diagnoser` is injected rather
    than constructed here so tests -- and a deliberately model-free run --
    use the same code path the real thing does.
    """
    detection = detect_revenue_at_risk(conn, merchant_id, source)
    conn.commit()

    prior_counts = {i.payment_attempt_id: i.prior_attempt_count for i in detection.items}

    items = claim_unprocessed_items(conn, detection.batch_id, limit=limit)

    results: list[RecoveryItemResult] = []
    for item in items:
        item = dict(item)
        item["prior_attempt_count"] = prior_counts.get(item["payment_attempt_id"], 0)
        try:
            results.append(_process_item(conn, merchant_id, detection.batch_id, item, diagnoser))
            conn.commit()  # durably persist this item's full outcome before the next
        except Exception as exc:  # noqa: BLE001 - one bad item must not abort the batch
            conn.rollback()
            results.append(
                RecoveryItemResult(
                    order_id=item["order_id"],
                    payment_attempt_id=item["payment_attempt_id"],
                    amount_at_risk=item["amount_at_risk"],
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    ledger = build_batch_ledger(conn, detection.batch_id)

    return RecoveryBatchResult(
        batch_id=detection.batch_id,
        merchant_id=merchant_id,
        source=detection.source,
        detected_count=detection.detected_count,
        revenue_at_risk=detection.revenue_at_risk,
        processed_count=len(results),
        items=results,
        ledger=ledger,
    )
