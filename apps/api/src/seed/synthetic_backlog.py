"""Generates a synthetic failed-payment backlog for one merchant.

WHY THIS EXISTS
    Razorpay Test Mode can create orders through the API, but authorising a
    payment requires a human to complete the Checkout flow in a browser.
    Producing forty real authorised payments by hand is not possible inside
    this project's timebox. So the batch that demonstrates triage at scale is
    generated locally, and the batch that demonstrates real money movement is
    the genuine Test Mode transaction. They are kept strictly separate.

WHAT MAKES THIS HONEST RATHER THAN DECORATIVE
    * Every row written here belongs to a batch whose `source` column is
      'synthetic'. That column is CHECK-constrained, carried on the ledger as
      `money_is_real`, and rendered in the UI. A synthetic outcome cannot be
      presented as real money recovered without deliberately editing several
      layers.
    * Synthetic payments are ONLY ever failed. A failed payment triggers no
      external call anywhere in the recovery pipeline -- the interventions it
      can select (retry prompt, escalate, stop) are all internal. So seeding
      this data cannot cause a Razorpay API call against a payment id that
      does not exist. The synthetic batch is provably side-effect-free.
    * Payment ids are prefixed `pay_SYN` and order ids `order_SYN`, not the
      `pay_`/`order_` shapes Razorpay issues, so a synthetic identifier is
      recognisable on sight and in any log.
    * The generator is seeded, so the same command produces the same backlog.
      A number quoted in a demo can be reproduced by a reviewer.

WHERE THE FAILURE EVIDENCE COMES FROM
    datasets/diagnosis/failure_corpus.json -- the same file the diagnosis
    layer reads. Sharing one source means a seeded payment's evidence always
    has a corresponding classification, rather than the two drifting apart and
    silently producing cache misses.

    Archetypes are sampled UNIFORMLY. The resulting mix is therefore not
    calibrated to any real merchant's failure distribution, and no claim is
    made that it is -- it is chosen for coverage of the decision paths, not
    for realism of proportions.

The decisions the recovery pipeline reaches over this data are real: the same
detection SQL, the same diagnosis lookup, the same deterministic engine, the
same policy gate, the same audit trail. Only the payments are fabricated.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from db.connection import get_connection
from diagnosis.precomputed import CORPUS_PATH
from repository.audit import insert_audit_entry
from repository.canonical_events import insert_canonical_event
from repository.orders import upsert_order
from repository.payment_attempts import get_payment_attempt, insert_payment_attempt

SYNTHETIC_ORDER_PREFIX = "order_SYN"
SYNTHETIC_PAYMENT_PREFIX = "pay_SYN"
DEFAULT_SEED = 20260904

# Order values spanning the policy bands, in paise. Weighted toward smaller
# values, as a real merchant's backlog is.
AMOUNT_BUCKETS: list[tuple[int, int, int]] = [
    (40, 50_000, 500_000),          # Rs 500 - Rs 5,000
    (30, 500_001, 1_000_000),       # Rs 5,000 - Rs 10,000
    (20, 1_000_001, 5_000_000),     # Rs 10,000 - Rs 50,000
    (10, 5_000_001, 25_000_000),    # Rs 50,000 - Rs 2,50,000
]


def load_corpus_entries() -> list[dict[str, Any]]:
    raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return raw["entries"]


def _pick_amount(rng: random.Random) -> int:
    weights = [b[0] for b in AMOUNT_BUCKETS]
    low, high = rng.choices([(b[1], b[2]) for b in AMOUNT_BUCKETS], weights=weights, k=1)[0]
    # Round to whole rupees -- a paise-level amount would look fabricated.
    return rng.randrange(low, high, 100)


def seed_synthetic_backlog(
    conn: psycopg.Connection, merchant_id: str, order_count: int = 40, seed: int = DEFAULT_SEED
) -> dict[str, Any]:
    """Writes `order_count` unpaid orders, each with one or more failed
    payment attempts, plus the canonical event for each attempt.

    Some orders get repeat attempts. That is what exercises the retry-budget
    stopping rule: an order attempted more times than the merchant's budget
    allows must stop regardless of what the diagnosis says about it.
    """
    rng = random.Random(seed)
    corpus = load_corpus_entries()
    now = datetime.now(timezone.utc)

    # Identifiers are scoped to the merchant. Without this, seeding a second
    # merchant reuses the first merchant's order ids: upsert_order() does not
    # reassign merchant_id, so the rows silently stay with merchant one, every
    # payment attempt is skipped as already-existing, and the second merchant
    # ends up with an empty backlog while the seeder cheerfully reports
    # success. Scoping the ids makes that collision impossible rather than
    # merely unlikely, and keeps the run reproducible for a given
    # (merchant, seed) pair.
    scope = merchant_id.replace("-", "")[:6]

    created_orders = 0
    created_attempts = 0
    skipped_attempts = 0
    total_at_risk = 0

    for index in range(order_count):
        order_id = f"{SYNTHETIC_ORDER_PREFIX}{scope}{index:04d}"
        amount = _pick_amount(rng)
        # 25% of orders have been attempted more than once. Those are the ones
        # that will hit the retry budget.
        attempt_count = rng.choices([1, 2, 3], weights=[75, 17, 8], k=1)[0]
        occurred_base = now - timedelta(hours=rng.randrange(1, 72))

        upsert_order(
            conn,
            order_id=order_id,
            merchant_id=merchant_id,
            amount=amount,
            amount_paid=0,
            amount_due=amount,
            status="attempted",
            attempts=attempt_count,
            currency="INR",
            raw_reference={"synthetic": True, "generator": "seed.synthetic_backlog", "seed": seed},
        )
        created_orders += 1
        total_at_risk += amount

        for attempt_index in range(attempt_count):
            entry = rng.choice(corpus)
            evidence = entry["evidence"]
            payment_id = f"{SYNTHETIC_PAYMENT_PREFIX}{scope}{index:04d}{attempt_index}"
            occurred_at = occurred_base + timedelta(minutes=attempt_index * 11)

            raw_reference = {
                "id": payment_id,
                "entity": "payment",
                "amount": amount,
                "currency": "INR",
                "status": "failed",
                "order_id": order_id,
                "method": evidence["method"],
                "bank": evidence.get("bank"),
                "wallet": evidence.get("wallet"),
                "error_code": evidence["error_code"],
                "error_description": evidence["error_description"],
                "error_source": evidence["error_source"],
                "error_step": evidence["error_step"],
                "error_reason": evidence["error_reason"],
                "synthetic": True,
                "corpus_entry_id": entry["id"],
                # The human label, for the evaluation harness only. Nothing in
                # the decision path reads it, and the model never saw it --
                # build_failure_signals' allowlist has no entry for it, so it
                # cannot reach a prompt even by accident.
                "ground_truth_failure_class": entry["ground_truth"]["failure_class"],
            }

            # Re-runnable: an existing attempt is left exactly as it is rather
            # than rewritten. payment_attempts is transition-guarded at the
            # database layer, and a seeder has no business pushing rows
            # through that guard.
            if get_payment_attempt(conn, payment_id) is not None:
                skipped_attempts += 1
            else:
                insert_payment_attempt(
                    conn,
                    payment_attempt_id=payment_id,
                    order_id=order_id,
                    status="failed",
                    method=evidence["method"],
                    captured=False,
                    error_source=evidence["error_source"],
                    error_step=evidence["error_step"],
                    error_reason=evidence["error_reason"],
                    amount=amount,
                    raw_reference=raw_reference,
                )
                created_attempts += 1

                event_id = insert_canonical_event(
                    conn,
                    merchant_id=merchant_id,
                    event_type="payment.attempt.failed",
                    source="razorpay_api_poll",
                    entity_type="payment",
                    entity_id=payment_id,
                    order_id=order_id,
                    occurred_at=occurred_at,
                    payload=raw_reference,
                    source_reference=f"synthetic:{seed}",
                )
                # Reconciliation writes EVENT_INGESTED for events it observes.
                # A seeded event is genuinely ingested too, just by this module
                # rather than by a Razorpay poll, so it gets the same opening
                # checkpoint -- otherwise every synthetic payment's audit trail
                # would begin mid-chain at AI_DIAGNOSIS_RECORDED and look
                # truncated next to a real one. The snapshot says plainly where
                # the event came from, so the two are still distinguishable.
                insert_audit_entry(
                    conn,
                    "EVENT_INGESTED",
                    {
                        "event_type": "payment.attempt.failed",
                        "entity_id": payment_id,
                        "ingested_by": "seed.synthetic_backlog",
                        "synthetic": True,
                    },
                    event_id=str(event_id),
                )

    conn.commit()
    return {
        "merchant_id": merchant_id,
        "orders_written": created_orders,
        "attempts_created": created_attempts,
        "attempts_skipped_already_present": skipped_attempts,
        "revenue_at_risk_estimate": total_at_risk,
        "corpus_entries_available": len(corpus),
        "seed": seed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a synthetic failed-payment backlog.")
    parser.add_argument("merchant_id", help="UUID of the merchant to seed against")
    parser.add_argument("--orders", type=int, default=40)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    with get_connection() as conn:
        summary = seed_synthetic_backlog(conn, args.merchant_id, args.orders, args.seed)

    print("Synthetic backlog seeded (SYNTHETIC -- no real money, no external calls):")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
