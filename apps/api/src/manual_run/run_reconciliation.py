"""Manual pipeline runner: reconciliation -> decision -> policy -> action
-> verification -> (optional) feedback calibration, for one merchant/order,
triggered by a human on the command line.

    python -m manual_run.run_reconciliation --merchant-id <id> --order-id <id> [--recalibrate]

ARCHITECTURAL INTENT: this module is tooling, not a new orchestration
layer. It contains no decision/policy/action/verification/calibration
logic of its own -- it only calls the same already-tested functions
every existing test already calls, in the same order Scenario A/B
already prove correct, and prints their results. A future FastAPI
endpoint, webhook handler, or scheduled worker must be able to call
`reconciliation.service.reconcile_order`, `intelligence.orchestration.
make_decision`, `action.orchestrator.propose_action`,
`verification.verifier.verify_action`, and
`feedback.calibration.recompute_baselines` directly, without ever
depending on this module. This module depends on the pipeline; the
pipeline must never depend on this module.

EVENT PROCESSING RULE: reconcile_order() returns exactly the newly
created canonical_events ids for this call. Only those ids are ever
fed into make_decision() -- the order's full historical event list is
never reprocessed. This is required for repeat-run safety:
make_decision() itself does not deduplicate (a changed mind is a new
Decision row, by design), so safety against duplicate decisions on a
second manual run comes entirely from only ever acting on genuinely
new event ids, never from re-walking history.

WRITE BOUNDARY: this module never imports or constructs
RazorpayWriteClient and never calls capture_payment() directly.
propose_action(..., write_client=None) is the sole path that may
construct a write client, exactly as it already does for every
existing test and Scenario A/B -- this module relies entirely on the
existing Policy -> Action authorization boundary and adds no bypass.

CALIBRATION: feedback.calibration.recompute_baselines() is never
called unless --recalibrate is passed, and even then only after every
newly returned event has finished processing. A calibration failure is
reported separately from, and never overwrites, an already-established
payment outcome printed earlier in the same run.

EXIT-STATUS CONTRACT (documented here since it is not an existing
project convention to extend, only db/run_migrations.py's plain
python -m precedent):
    0   the requested run completed -- this includes a run where Policy
        BLOCKED an action or an action reached APPROVAL_PENDING, since
        those are legitimate business outcomes the runner correctly
        observed and reported, not failures of the runner itself.
    1   the runner could not complete the requested orchestration:
        missing credentials, merchant not found, a Razorpay read
        failure, an unresolved reconciliation-returned event id, or (only
        when --recalibrate was explicitly passed) a calibration failure.

OUTPUT: one concise line per stage/event (see the module's print calls
below for the exact vocabulary). Never prints credentials, DATABASE_URL,
raw request/response bodies, full exception objects, or an internal
object's repr() -- only status strings and counts that are already safe
to display (the same fields observability/evaluation already treat as
safe to serialize).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import psycopg

from action.orchestrator import propose_action
from db.connection import get_connection
from feedback.calibration import recompute_baselines
from intelligence.orchestration import make_decision
from policy.orchestration import NotPolicyGated
from razorpay_client.client import RazorpayReadClient
from razorpay_client.errors import RazorpayAPIError
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order
from repository.decisions import get_decision
from repository.merchants import get_merchant
from verification.verifier import verify_action

EXIT_OK = 0
EXIT_OPERATIONAL_ERROR = 1

_NO_ACTION_DECISION_TYPE = "NO_ACTION"
_VERIFYING_ACTION_STATUS = "VERIFYING"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m manual_run.run_reconciliation",
        description=(
            "Manually run reconciliation -> decision -> policy -> action -> "
            "verification (and optionally feedback calibration) for one merchant/order."
        ),
    )
    parser.add_argument("--merchant-id", required=True, help="Existing merchants.id (UUID).")
    parser.add_argument("--order-id", required=True, help="Razorpay order_... id.")
    parser.add_argument(
        "--recalibrate", action="store_true",
        help="After processing, recompute expectation_baselines for this merchant (feedback.calibration.recompute_baselines).",
    )
    return parser.parse_args(argv)


def _process_event(conn: psycopg.Connection, merchant_id: str, event: dict[str, Any], read_client: Any) -> None:
    print(f"event: {event['id']} {event['event_type']}")

    decision_id = make_decision(conn, merchant_id, event)
    decision = get_decision(conn, decision_id)
    print(f"decision: {decision['decision_type']}")

    if decision["decision_type"] == _NO_ACTION_DECISION_TYPE:
        print("action: not proposed (NO_ACTION)")
        return

    try:
        action = propose_action(conn, decision_id, write_client=None)
    except NotPolicyGated:
        print("action: not proposed (decision_type is not policy-gated)")
        return

    print(f"action: {action['status']}")

    if action["status"] == _VERIFYING_ACTION_STATUS:
        final_action = verify_action(conn, action["id"], read_client=read_client)
        print(f"verification: {final_action['status']}")
    else:
        print("verification: skipped (action did not reach VERIFYING)")


def run(conn: psycopg.Connection, read_client: Any, merchant_id: str, order_id: str, recalibrate: bool) -> int:
    merchant = get_merchant(conn, merchant_id)
    if merchant is None:
        print(f"error: merchant {merchant_id} not found")
        return EXIT_OPERATIONAL_ERROR
    print(f"merchant: {merchant_id} validated")

    try:
        new_event_ids = reconcile_order(conn, read_client, merchant_id, order_id)
    except RazorpayAPIError:
        print("error: Razorpay read failed for this order")
        return EXIT_OPERATIONAL_ERROR

    print(f"reconciliation: {len(new_event_ids)} new event(s)")

    if new_event_ids:
        events_by_id = {str(e["id"]): e for e in list_events_for_order(conn, order_id)}
        for event_id in new_event_ids:
            event = events_by_id.get(str(event_id))
            if event is None:
                print(f"error: reconciliation returned event {event_id}, which could not be resolved -- stopping")
                return EXIT_OPERATIONAL_ERROR
            _process_event(conn, merchant_id, event, read_client)
            conn.commit()  # durably persist this event's full outcome before moving on
    else:
        print("run: nothing new to process")

    if recalibrate:
        try:
            report = recompute_baselines(conn, merchant_id)
            conn.commit()
            print(f"calibration: completed ({report.buckets_processed} bucket(s) recomputed)")
        except Exception:
            conn.rollback()
            print("calibration: failed -- payment outcomes reported above are unaffected")
            return EXIT_OPERATIONAL_ERROR

    print("run: completed")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        read_client = RazorpayReadClient()
    except RuntimeError as exc:
        print(f"error: {exc}")
        return EXIT_OPERATIONAL_ERROR

    try:
        with get_connection() as conn:
            return run(conn, read_client, args.merchant_id, args.order_id, args.recalibrate)
    finally:
        read_client.close()


if __name__ == "__main__":
    sys.exit(main())
