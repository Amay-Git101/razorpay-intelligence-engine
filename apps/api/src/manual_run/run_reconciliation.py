"""Manual pipeline runner: reconciliation -> decision -> policy -> action
-> verification -> (optional) feedback calibration, for one merchant/order,
triggered by a human on the command line.

    python -m manual_run.run_reconciliation --merchant-id <id> --order-id <id> [--recalibrate]

ARCHITECTURAL INTENT: this module is a thin CLI presentation layer over
pipeline.orchestration.run_reconciliation_pipeline() -- the actual
reconcile -> decide -> policy -> action -> verify sequencing lives
there now, shared with the HTTP API, so neither this CLI nor the API
depends on the other. This module's only remaining job is argument
parsing, merchant validation, credential setup, the --recalibrate step,
and turning a PipelineRunResult into human-readable lines.

WRITE BOUNDARY: this module never imports or constructs
RazorpayWriteClient and never calls capture_payment() directly.
propose_action(..., write_client=None), called inside
pipeline.orchestration, remains the sole path that may construct a
write client -- this module relies entirely on the existing
Policy -> Action authorization boundary and adds no bypass.

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

OUTPUT: one concise line per stage/event. Never prints credentials,
DATABASE_URL, raw request/response bodies, full exception objects, or
an internal object's repr() -- only status strings and counts that are
already safe to display.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import psycopg

from db.connection import get_connection
from feedback.calibration import recompute_baselines
from pipeline.orchestration import PipelineRunResult, UnresolvedEventError, run_reconciliation_pipeline
from razorpay_client.client import RazorpayReadClient
from razorpay_client.errors import RazorpayAPIError
from repository.merchants import get_merchant

EXIT_OK = 0
EXIT_OPERATIONAL_ERROR = 1


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


def _print_pipeline_result(result: PipelineRunResult) -> None:
    print(f"reconciliation: {result.new_event_count} new event(s)")
    if not result.events:
        print("run: nothing new to process")
        return

    for event in result.events:
        print(f"event: {event.event_id} {event.event_type}")
        print(f"decision: {event.decision_type}")
        if event.action_skipped_reason is not None:
            print(f"action: not proposed ({event.action_skipped_reason})")
            continue
        print(f"action: {event.action_status}")
        if event.verification_status is not None:
            print(f"verification: {event.verification_status}")
        else:
            print("verification: skipped (action did not reach VERIFYING)")


def run(conn: psycopg.Connection, read_client: Any, merchant_id: str, order_id: str, recalibrate: bool) -> int:
    merchant = get_merchant(conn, merchant_id)
    if merchant is None:
        print(f"error: merchant {merchant_id} not found")
        return EXIT_OPERATIONAL_ERROR
    print(f"merchant: {merchant_id} validated")

    try:
        result = run_reconciliation_pipeline(conn, read_client, merchant_id, order_id)
    except RazorpayAPIError:
        print("error: Razorpay read failed for this order")
        return EXIT_OPERATIONAL_ERROR
    except UnresolvedEventError:
        print("error: reconciliation returned an event that could not be resolved -- stopping")
        return EXIT_OPERATIONAL_ERROR

    _print_pipeline_result(result)

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
