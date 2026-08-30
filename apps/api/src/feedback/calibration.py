"""Verified CAPTURE_PAYMENT outcome -> expectation_baselines feedback.

This closes exactly one narrow piece of the Outcome -> Feedback ->
Expectation loop described in the project's target architecture: it
reads already-committed, terminal Verification outcomes for
CAPTURE_PAYMENT actions and recomputes the corresponding
expectation_baselines rows from scratch.

SCOPE -- read this before assuming this module does more than it does:

  - Covers CAPTURE_PAYMENT actions only. RECOMMEND_RETRY_PROMPT /
    CUSTOMER_RETRY_PROMPT outcomes are NOT covered and are NOT
    calibrated here. CUSTOMER_RETRY_PROMPT currently never reaches
    Verification (it stops at AUTHORIZED by design -- see
    action/orchestrator.py), and no trustworthy negative/terminal
    outcome exists for it anywhere in this schema. No timeout,
    abandonment heuristic, or "not yet paid means failed" proxy is
    used here or anywhere else -- that remains explicitly deferred.
  - RECOMMEND_CAPTURE's decision_type and confidence (fixed at 1.0) do
    not read Expectation at all and are entirely unaffected by
    anything this module writes. Writing a baseline here does not
    change what RuleBasedEngine recommends or how confident it claims
    to be -- this is data-feedback infrastructure, not a decision-rule
    change.
  - Nothing in this module is wired to any caller. No scheduler, no
    CLI, no HTTP endpoint, no hook from verify_action() or
    reconcile_order() exists. recompute_baselines() is a plain,
    callable, idempotent function -- correct and tested, currently
    unreachable from any live path, exactly like reconcile_order() was
    before its own eventual caller exists.
  - Makes no statistical claim. The word "accuracy" never applies here.
    A recovery_rate computed from a handful of observations is not
    "calibration quality" -- expectation_baselines.sample_size (already
    an existing, unmodified field) remains the signal of how much
    evidence backs a number; this module does not add or change any
    minimum-sample gating (intelligence/expectation.py is untouched).

EVIDENCE UNIT: one terminal actions row (status in {VERIFIED_SUCCESS,
VERIFIED_FAILED}) with action_type='CAPTURE_PAYMENT' is exactly one
observation. ESCALATED (Verification could not determine an outcome),
BLOCKED (Policy prevented execution -- nothing was tested), and every
non-terminal status (AUTHORIZED/EXECUTING/VERIFYING/
VERIFICATION_UNCERTAIN) are excluded by the query itself, not by a
post-hoc filter. CUSTOMER_RETRY_PROMPT actions are excluded by the
action_type filter. A verification read is never itself an
observation -- only the row's final terminal status is read, once.

IDEMPOTENCY: full deterministic recomputation, not incremental
accumulation. Every call recomputes success_count/failure_count/
recovery_rate from the current actions/decisions rows and overwrites
(not increments) the relevant expectation_baselines row via the
existing, unmodified intelligence.calibration.upsert_calibrated_baseline().
Running this twice with no new terminal actions produces byte-identical
results. This is also the entire backfill mechanism -- there is no
separate backfill path, because a fresh run over historical rows and a
fresh run over newly-added rows are the same operation.

BUCKET: grouped by (decisions.merchant_id, decisions.expectation
->>'bucket_key') -- the bucket_key already persisted at decision time
by intelligence/orchestration.py's _bucket_key_for_context(), read
verbatim, never reconstructed from context and never combined with any
other dimension (error_source, method, amount, attempt_number). Adding
such a dimension here would produce a bucket_key compute_expectation()
can never look up, since that function's own key format is untouched
by this gate.

TRANSACTION SAFETY: this module never touches actions or decisions
with a write statement -- it only SELECTs from them. It cannot, by
construction, alter an already-committed verification outcome. Its own
read+write pair runs inside one savepoint-scoped `conn.transaction()`
block, entirely separate from whatever transaction produced the
outcome (which has already committed by the time this is ever called
for real). A failure here propagates to the caller -- it is never
caught and swallowed -- and rolls back only this call's own
expectation_baselines writes, leaving both the source rows and any
previously-committed baselines untouched.

ZERO-EVIDENCE CASE: a (merchant, bucket) with no terminal CAPTURE_PAYMENT
observations produces no row in the aggregation query at all, and
therefore no baseline is written for it -- compute_expectation() keeps
returning the existing zero-evidence default for that bucket. No
zero-observation baseline row is ever manufactured.
"""

from __future__ import annotations

import psycopg
from pydantic import BaseModel
from psycopg.rows import dict_row

from intelligence.calibration import upsert_calibrated_baseline

CAPTURE_PAYMENT_ACTION_TYPE = "CAPTURE_PAYMENT"
TERMINAL_STATUSES = ("VERIFIED_SUCCESS", "VERIFIED_FAILED")


class BucketRecoveryObservation(BaseModel):
    """One (merchant, bucket) aggregate from this run. Not a claim about
    calibration quality -- see calibration.py's module docstring."""

    merchant_id: str
    bucket_key: str
    verified_success_count: int
    verified_failed_count: int
    total_terminal_observations: int
    recovery_rate: float
    baseline_written: bool


class FeedbackReport(BaseModel):
    """Result of one recompute_baselines() run. Inspectable, not a
    statistical claim -- see calibration.py's module docstring for what
    this deliberately does not cover or assert."""

    merchants_processed: list[str]
    buckets_processed: int
    total_terminal_observations: int
    total_verified_success: int
    total_verified_failed: int
    bucket_results: list[BucketRecoveryObservation]
    scope_note: str = (
        "Covers verified CAPTURE_PAYMENT outcomes only. Does not cover "
        "RECOMMEND_RETRY_PROMPT / CUSTOMER_RETRY_PROMPT (no trustworthy "
        "terminal outcome exists for that action type in the current "
        "system). Does not change RECOMMEND_CAPTURE's decision_type or "
        "its confidence, which remains fixed at 1.0 regardless of any "
        "baseline written here. Makes no statistical calibration-quality "
        "claim and no AI-attributed-revenue claim -- see this module's "
        "docstring for the full scope."
    )


def recompute_baselines(conn: psycopg.Connection, merchant_id: str | None = None) -> FeedbackReport:
    """Reads terminal CAPTURE_PAYMENT verification outcomes and
    overwrites the corresponding expectation_baselines rows with a
    freshly recomputed aggregate. Pass merchant_id to scope one tenant;
    omit it to recompute for every merchant with terminal observations
    in one call (never merging observations across merchants either
    way -- every group is per-merchant, per-bucket).

    Idempotent: rerunning with no new terminal actions since the last
    run produces identical output and does not double-count anything.
    Doubles as the historical-backfill path -- there is no separate one.
    """
    # The two terminal statuses are fixed, not caller-supplied, so they
    # are embedded as SQL literals -- an already-established pattern
    # elsewhere in this codebase's read-only aggregation queries --
    # rather than risking ambiguity in how a Python tuple/list adapts to
    # an `IN` clause.
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            if merchant_id is None:
                cur.execute(
                    """
                    select
                        d.merchant_id::text as merchant_id,
                        d.expectation->>'bucket_key' as bucket_key,
                        count(*) filter (where a.status = 'VERIFIED_SUCCESS') as success_count,
                        count(*) filter (where a.status = 'VERIFIED_FAILED') as failure_count
                    from actions a
                    join decisions d on a.decision_id = d.id
                    where a.action_type = %s
                      and a.status in ('VERIFIED_SUCCESS', 'VERIFIED_FAILED')
                    group by d.merchant_id, d.expectation->>'bucket_key'
                    order by d.merchant_id::text, bucket_key
                    """,
                    (CAPTURE_PAYMENT_ACTION_TYPE,),
                )
            else:
                cur.execute(
                    """
                    select
                        d.merchant_id::text as merchant_id,
                        d.expectation->>'bucket_key' as bucket_key,
                        count(*) filter (where a.status = 'VERIFIED_SUCCESS') as success_count,
                        count(*) filter (where a.status = 'VERIFIED_FAILED') as failure_count
                    from actions a
                    join decisions d on a.decision_id = d.id
                    where a.action_type = %s
                      and a.status in ('VERIFIED_SUCCESS', 'VERIFIED_FAILED')
                      and d.merchant_id = %s
                    group by d.merchant_id, d.expectation->>'bucket_key'
                    order by d.merchant_id::text, bucket_key
                    """,
                    (CAPTURE_PAYMENT_ACTION_TYPE, merchant_id),
                )
            rows = cur.fetchall()

        bucket_results: list[BucketRecoveryObservation] = []
        for row in rows:
            success_count = row["success_count"]
            failure_count = row["failure_count"]
            total = success_count + failure_count
            recovery_rate = success_count / total

            upsert_calibrated_baseline(conn, row["merchant_id"], row["bucket_key"], recovery_rate, total)

            bucket_results.append(
                BucketRecoveryObservation(
                    merchant_id=row["merchant_id"],
                    bucket_key=row["bucket_key"],
                    verified_success_count=success_count,
                    verified_failed_count=failure_count,
                    total_terminal_observations=total,
                    recovery_rate=recovery_rate,
                    baseline_written=True,
                )
            )

    merchants_processed = sorted({r.merchant_id for r in bucket_results})
    return FeedbackReport(
        merchants_processed=merchants_processed,
        buckets_processed=len(bucket_results),
        total_terminal_observations=sum(r.total_terminal_observations for r in bucket_results),
        total_verified_success=sum(r.verified_success_count for r in bucket_results),
        total_verified_failed=sum(r.verified_failed_count for r in bucket_results),
        bucket_results=bucket_results,
    )
