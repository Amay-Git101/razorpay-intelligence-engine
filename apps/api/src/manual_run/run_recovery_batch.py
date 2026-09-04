"""Operator CLI: run the recovery workflow across a merchant's at-risk backlog.

    python -m manual_run.run_recovery_batch <merchant_id> --source synthetic

Deliberately a CLI and not an HTTP endpoint. Running a batch calls a language
model once per at-risk payment, and on a real batch it can move money through
Razorpay. Neither belongs behind a button that an anonymous visitor to the
demo can press repeatedly. The API exposes the RESULTS of a batch, read-only;
producing one is an operator action that happens here.

Like run_reconciliation.py, this runner is deliberately thin. It does not call
RazorpayWriteClient, contains no SQL, and holds no business rules -- it parses
arguments, constructs the diagnoser, calls
pipeline.recovery.run_recovery_batch(), and prints. Every decision it reports
was made by the same shared code path the API and the tests exercise.

--no-model runs the identical pipeline with no diagnoser at all. That is not a
degraded mode bolted on for convenience: it is how you demonstrate that an
absent model produces MORE human escalation rather than more automation. Both
runs are worth showing.
"""

from __future__ import annotations

import argparse

from db.connection import get_connection
from diagnosis.diagnoser import AnthropicDiagnoser
from diagnosis.precomputed import PrecomputedDiagnoser
from pipeline.recovery import run_recovery_batch
from risk.detection import SOURCE_RAZORPAY_TEST_MODE, SOURCE_SYNTHETIC


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a bounded recovery batch for one merchant.")
    parser.add_argument("merchant_id", help="UUID of the merchant")
    parser.add_argument(
        "--source",
        required=True,
        choices=[SOURCE_SYNTHETIC, SOURCE_RAZORPAY_TEST_MODE],
        help=(
            "Declares whether this batch's money is real. Required, with no default -- "
            "a batch that does not say cannot be created."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N items (largest amounts first)")
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="Run with no diagnoser at all. Every failed payment should escalate to a human.",
    )
    parser.add_argument(
        "--live-model",
        action="store_true",
        help=(
            "Call the Anthropic API for each diagnosis instead of replaying the precomputed "
            "corpus. Requires ANTHROPIC_API_KEY. Identical pipeline either way -- only the "
            "diagnoser implementation differs."
        ),
    )
    args = parser.parse_args()

    # The default is the offline corpus. This project has no runtime API key,
    # and replaying a stored classification is the honest alternative to
    # pretending otherwise. --live-model swaps in the API-backed implementation
    # of the same protocol and changes nothing else about the run.
    if args.no_model:
        diagnoser = None
    elif args.live_model:
        diagnoser = AnthropicDiagnoser()
    else:
        diagnoser = PrecomputedDiagnoser()

    with get_connection() as conn:
        result = run_recovery_batch(
            conn, args.merchant_id, source=args.source, diagnoser=diagnoser, limit=args.limit
        )

    print(f"\nBatch {result.batch_id}")
    print(f"  source            : {result.source}")
    if result.source == SOURCE_SYNTHETIC:
        print("                      SYNTHETIC -- decision simulation, no real money, no external calls")
    print(f"  detected          : {result.detected_count} at-risk payments")
    print(f"  revenue at risk   : {_rupees(result.revenue_at_risk)}")
    print(f"  processed         : {result.processed_count}")

    if result.ledger:
        print("\n  Disposition of the money at risk:")
        for bucket in result.ledger.at_risk_by_outcome:
            share = (bucket.amount / result.ledger.revenue_at_risk * 100) if result.ledger.revenue_at_risk else 0.0
            print(f"    {bucket.category:<20} {bucket.count:>4} items  {_rupees(bucket.amount):>18}  ({share:4.1f}%)")
        print(
            f"\n  Independently verified recovery: {_rupees(result.ledger.verified_recovered_amount)} "
            f"across {result.ledger.verified_recovered_count} captures"
        )
        if not result.ledger.disposition_is_complete:
            print("  WARNING: outcome buckets do not sum to revenue at risk -- the ledger is inconsistent")

    errors = [item for item in result.items if item.error]
    if errors:
        print(f"\n  {len(errors)} item(s) failed to process:")
        for item in errors[:10]:
            print(f"    {item.payment_attempt_id}: {item.error}")


if __name__ == "__main__":
    main()
