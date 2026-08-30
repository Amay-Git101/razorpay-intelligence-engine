"""Read-only aggregation over already-persisted decisions/actions data.

This module never writes to the database, never calls Razorpay, and never
feeds back into any runtime decision path (RuleBasedEngine / Policy /
Action / Verification). It exists purely so a human can look at what the
system has already recorded.

Every function below is merchant-scoped, queries `decisions` and/or
`actions` directly (joined via `actions.decision_id -> decisions.id`,
since `actions` carries no merchant_id column of its own), and returns a
typed Pydantic model whose docstring states both what the figure means
and what it deliberately does NOT mean.

System-wide limitations that apply across every metric here (see the
Evaluation + Observability design inspection for the full reasoning):

  - Decision correctness (whether a recommendation was the *right* one)
    is not measurable anywhere in this module. No ground-truth label for
    "this decision was correct" is persisted anywhere in this system.
  - No stage-level latency (event -> decision, decision -> action
    proposed, proposed -> authorized, etc.) is computed here. Every
    timestamp column in this schema (created_at / updated_at /
    observed_at / ingested_at) defaults to Postgres now(), which is
    scoped to the enclosing transaction, not the individual statement --
    see 0004_audit_entries_ordering_sequence.sql for the full root-cause
    explanation. The one exception is
    `actions.outcome.time_to_resolution_seconds`, computed in
    application code with a real wall-clock read (see
    verification/verifier.py's _finalize), which is the only duration
    figure this module reports.
  - No calibration/backtest metric is produced: `expected_recovery_rate`
    in a persisted Expectation is either a hand-set baseline or the
    arbitrary zero-evidence prior (rule_v1_default) -- nothing in this
    system writes real outcomes back into expectation_baselines, so
    there is no historical-outcome-to-baseline linkage to check.
"""

from __future__ import annotations

import psycopg
from pydantic import BaseModel

# The only action_type in the current decision_type -> action_type
# mapping (see src/policy/orchestration.py) with moves_money=True. Used
# to scope every "money-moving" / capture-specific metric below.
_CAPTURE_ACTION_TYPE = "CAPTURE_PAYMENT"
_RETRY_PROMPT_ACTION_TYPE = "CUSTOMER_RETRY_PROMPT"


# ---------------------------------------------------------------------------
# A. Decision-type distribution
# ---------------------------------------------------------------------------

class DecisionTypeDistribution(BaseModel):
    """Counts of persisted `decisions` rows grouped by `decision_type`,
    for one merchant.

    Meaning: what the decision engine recommended.
    Does NOT mean: whether any recommendation was correct -- no
    ground-truth label for decision correctness is persisted anywhere in
    this system.
    """

    merchant_id: str
    counts: dict[str, int]


def decision_type_distribution(conn: psycopg.Connection, merchant_id: str) -> DecisionTypeDistribution:
    with conn.cursor() as cur:
        cur.execute(
            "select decision_type, count(*) from decisions where merchant_id = %s "
            "group by decision_type order by decision_type",
            (merchant_id,),
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
    return DecisionTypeDistribution(merchant_id=merchant_id, counts=counts)


# ---------------------------------------------------------------------------
# B. Policy-outcome distribution for money-moving decisions
# ---------------------------------------------------------------------------

class PolicyOutcomeDistribution(BaseModel):
    """How Policy classified CAPTURE_PAYMENT proposals -- the only
    money-moving action_type in this system's current
    decision_type -> action_type mapping -- for one merchant, read
    directly from the persisted `PolicyEvaluation`
    (`actions.policy_evaluation.allowed` /
    `actions.policy_evaluation.requires_approval`), never inferred from
    a later action status.

    Meaning: how Policy treated money-moving recommendations.
    Does NOT mean: whether the underlying Decision was correct.
    """

    merchant_id: str
    allow: int
    approval_required: int
    block: int


def policy_outcome_distribution(conn: psycopg.Connection, merchant_id: str) -> PolicyOutcomeDistribution:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                count(*) filter (
                    where (a.policy_evaluation->>'allowed')::boolean = true
                      and (a.policy_evaluation->>'requires_approval')::boolean = false
                ),
                count(*) filter (
                    where (a.policy_evaluation->>'allowed')::boolean = true
                      and (a.policy_evaluation->>'requires_approval')::boolean = true
                ),
                count(*) filter (
                    where (a.policy_evaluation->>'allowed')::boolean = false
                )
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        allow, approval_required, block = cur.fetchone()
    return PolicyOutcomeDistribution(merchant_id=merchant_id, allow=allow, approval_required=approval_required, block=block)


# ---------------------------------------------------------------------------
# C. Capture-action terminal-status distribution
# ---------------------------------------------------------------------------

class CaptureTerminalStatusDistribution(BaseModel):
    """Terminal-state counts for CAPTURE_PAYMENT actions only, for one
    merchant. CUSTOMER_RETRY_PROMPT actions are never included here --
    see RetryPromptOutcomeAvailability below for why no comparable
    distribution exists for that action_type.

    `blocked` is a Policy-prevention outcome and is kept visibly
    separate from the three genuine Verification outcomes
    (`verified_success` / `verified_failed` / `escalated`) -- a blocked
    capture is NOT a verification failure. Never sum `blocked` together
    with `verified_failed` or `escalated` to produce a "failure rate".
    """

    merchant_id: str
    verified_success: int
    verified_failed: int
    escalated: int
    blocked: int


def capture_terminal_status_distribution(conn: psycopg.Connection, merchant_id: str) -> CaptureTerminalStatusDistribution:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                count(*) filter (where a.status = 'VERIFIED_SUCCESS'),
                count(*) filter (where a.status = 'VERIFIED_FAILED'),
                count(*) filter (where a.status = 'ESCALATED'),
                count(*) filter (where a.status = 'BLOCKED')
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        verified_success, verified_failed, escalated, blocked = cur.fetchone()
    return CaptureTerminalStatusDistribution(
        merchant_id=merchant_id, verified_success=verified_success, verified_failed=verified_failed,
        escalated=escalated, blocked=blocked,
    )


# ---------------------------------------------------------------------------
# D. Escalation metrics
# ---------------------------------------------------------------------------

class EscalationMetrics(BaseModel):
    """Escalation counts for CAPTURE_PAYMENT actions, for one merchant,
    sourced from the persisted Verification `verification_result.reason`
    -- never inferred from generic action status.
    """

    merchant_id: str
    total_escalated: int
    by_reason: dict[str, int]


def escalation_metrics(conn: psycopg.Connection, merchant_id: str) -> EscalationMetrics:
    with conn.cursor() as cur:
        cur.execute(
            """
            select coalesce(a.verification_result->>'reason', 'UNKNOWN'), count(*)
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s and a.status = 'ESCALATED'
            group by 1
            order by 1
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        by_reason = {row[0]: row[1] for row in cur.fetchall()}
    return EscalationMetrics(merchant_id=merchant_id, total_escalated=sum(by_reason.values()), by_reason=by_reason)


# ---------------------------------------------------------------------------
# E. Verification read-attempt distribution
# ---------------------------------------------------------------------------

class VerificationReadAttemptDistribution(BaseModel):
    """Counts of CAPTURE_PAYMENT actions grouped by how many live
    Razorpay read attempts Verification made
    (`verification_result.attempt_count`), for one merchant. Only
    actions that actually entered Verification
    (`verification_result is not null`) are counted -- an action that
    never reached VERIFYING contributes nothing here, not a zero.
    """

    merchant_id: str
    by_attempt_count: dict[int, int]


def verification_read_attempt_distribution(conn: psycopg.Connection, merchant_id: str) -> VerificationReadAttemptDistribution:
    with conn.cursor() as cur:
        cur.execute(
            """
            select (a.verification_result->>'attempt_count')::int, count(*)
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s and a.verification_result is not null
            group by 1
            order by 1
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        by_attempt_count = {row[0]: row[1] for row in cur.fetchall()}
    return VerificationReadAttemptDistribution(merchant_id=merchant_id, by_attempt_count=by_attempt_count)


# ---------------------------------------------------------------------------
# F. Verified captured amount
# ---------------------------------------------------------------------------

class VerifiedCapturedAmount(BaseModel):
    """VERIFIED CAPTURED AMOUNT -- the total `outcome.recovered_amount`
    across CAPTURE_PAYMENT actions that reached VERIFIED_SUCCESS, for one
    merchant. This is the amount Verification independently confirmed as
    captured via a live Razorpay read.

    This figure is NOT: AI-attributed recovery, decision-created
    revenue, model value, or decision accuracy. Whether a capture was
    even attempted was Policy's decision, not RuleBasedEngine's -- see
    PolicyOutcomeDistribution.
    """

    merchant_id: str
    verified_success_count: int
    total_verified_captured_amount: int


def verified_captured_amount(conn: psycopg.Connection, merchant_id: str) -> VerifiedCapturedAmount:
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*), coalesce(sum((a.outcome->>'recovered_amount')::bigint), 0)
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s and a.status = 'VERIFIED_SUCCESS'
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        verified_success_count, total = cur.fetchone()
    return VerifiedCapturedAmount(
        merchant_id=merchant_id, verified_success_count=verified_success_count, total_verified_captured_amount=total,
    )


# ---------------------------------------------------------------------------
# G. Verification resolution time
# ---------------------------------------------------------------------------

class VerificationResolutionTiming(BaseModel):
    """Resolution-time statistics for CAPTURE_PAYMENT actions that
    reached VERIFIED_SUCCESS, for one merchant, computed ONLY from the
    already-persisted `outcome.time_to_resolution_seconds` values
    (an application-computed wall-clock duration -- see
    verification/verifier.py's _finalize). No figure here is derived
    from created_at / updated_at / any audit_entries timestamp; those
    are transaction-scoped Postgres now() values and cannot support
    duration arithmetic (see 0004_audit_entries_ordering_sequence.sql).

    min_seconds / max_seconds / avg_seconds are None, not 0, when count
    is 0 -- an absent figure must never be mistaken for a real
    zero-second resolution.
    """

    merchant_id: str
    count: int
    min_seconds: float | None
    max_seconds: float | None
    avg_seconds: float | None


def verification_resolution_timing(conn: psycopg.Connection, merchant_id: str) -> VerificationResolutionTiming:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                count(*),
                min((a.outcome->>'time_to_resolution_seconds')::double precision),
                max((a.outcome->>'time_to_resolution_seconds')::double precision),
                avg((a.outcome->>'time_to_resolution_seconds')::double precision)
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s and a.status = 'VERIFIED_SUCCESS'
            """,
            (merchant_id, _CAPTURE_ACTION_TYPE),
        )
        count, min_seconds, max_seconds, avg_seconds = cur.fetchone()
    return VerificationResolutionTiming(
        merchant_id=merchant_id,
        count=count,
        min_seconds=float(min_seconds) if min_seconds is not None else None,
        max_seconds=float(max_seconds) if max_seconds is not None else None,
        avg_seconds=float(avg_seconds) if avg_seconds is not None else None,
    )


# ---------------------------------------------------------------------------
# CUSTOMER_RETRY_PROMPT outcome -- explicitly unavailable, not a silent zero
# ---------------------------------------------------------------------------

class RetryPromptOutcomeAvailability(BaseModel):
    """Whether CUSTOMER_RETRY_PROMPT actions carry any outcome data, for
    one merchant. `outcome_measurable` is always False in the current
    implementation: the Action module deliberately stops
    CUSTOMER_RETRY_PROMPT actions at AUTHORIZED and never dispatches
    them to execution or Verification (see the module docstring of
    src/action/orchestrator.py) -- so no terminal status,
    execution_reference outcome, or verification_result is ever produced
    for this action_type.

    `total_customer_retry_prompt_actions` is a real, persisted count of
    how many such actions exist -- it describes volume, not outcome. A
    caller must not mistake this model's presence for evidence that
    retry-prompt effectiveness is measurable; it explicitly is not.
    """

    merchant_id: str
    total_customer_retry_prompt_actions: int
    outcome_measurable: bool = False
    reason: str = (
        "CUSTOMER_RETRY_PROMPT actions never leave AUTHORIZED in the current "
        "Action implementation -- no execution, verification, or outcome data "
        "is ever produced for this action_type. This is not a zero-observation "
        "count; the concept is not yet measurable."
    )


def retry_prompt_outcome_availability(conn: psycopg.Connection, merchant_id: str) -> RetryPromptOutcomeAvailability:
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*)
            from actions a
            join decisions d on a.decision_id = d.id
            where d.merchant_id = %s and a.action_type = %s
            """,
            (merchant_id, _RETRY_PROMPT_ACTION_TYPE),
        )
        (total,) = cur.fetchone()
    return RetryPromptOutcomeAvailability(merchant_id=merchant_id, total_customer_retry_prompt_actions=total)
