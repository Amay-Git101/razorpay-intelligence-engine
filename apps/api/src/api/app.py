"""Minimal HTTP API surface for the frontend.

    GET  /health
    GET  /merchants
    GET  /merchants/{merchant_id}/payments
    GET  /merchants/{merchant_id}/metrics
    GET  /orders/{order_id}
    GET  /orders/{order_id}/timeline
    POST /merchants/{merchant_id}/orders/{order_id}/reconcile
    GET  /merchants/{merchant_id}/recovery-batches
    GET  /recovery-batches/{batch_id}
    POST /decision-lab/simulate

Run locally with:
    .venv/Scripts/python -m uvicorn api.app:app --reload

ARCHITECTURE: this module is a leaf/delivery layer. It contains no
decision/policy/action/verification/calibration logic and no SQL of
its own -- every endpoint calls an existing repository read function,
an existing observability read function, or
pipeline.orchestration.run_reconciliation_pipeline() (the same shared
function the project's CLI tooling calls). No existing backend module
imports this package; a mechanical test in
test_architecture_boundaries.py proves that.

MONEY-MOVING SAFETY: this module never imports or constructs
RazorpayWriteClient, never calls capture_payment(), and never
auto-approves an APPROVAL_PENDING action -- the human-approval
functions in action/orchestrator.py are not imported here at all. The
only Razorpay-adjacent call is
RazorpayReadClient(), used exclusively by the reconcile endpoint via
run_reconciliation_pipeline(); propose_action(..., write_client=None),
inside that shared function, remains the sole path that may construct
a write client. No endpoint accepts a Razorpay credential or a
DATABASE_URL value in its request.

ERROR HANDLING: known conditions map to HTTP status codes (404 for a
missing merchant/order, 400 for a malformed identifier, 502 for a
Razorpay read failure, 500 for a server misconfiguration or an
otherwise-unexpected failure) with a short, credential-safe detail
string -- never a raw exception, traceback, or object repr().
"""

from __future__ import annotations

import os
import uuid as uuid_module
from pathlib import Path
from typing import Any, Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db.connection import get_connection
from observability.batch_ledger import build_batch_ledger
from observability.metrics import (
    capture_terminal_status_distribution,
    decision_type_distribution,
    escalation_metrics,
    policy_outcome_distribution,
    retry_prompt_outcome_availability,
    verification_read_attempt_distribution,
    verification_resolution_timing,
    verified_captured_amount,
)
from context.customer_history import summarize_customer_history
from pipeline.orchestration import UnresolvedEventError, run_reconciliation_pipeline
from pipeline.simulation import simulate_decision
from provisioning.razorpay_order_client import RazorpayOrderClient
from provisioning.test_orders import CohortAlreadyInUse, create_test_orders
from risk.failure_patterns import FailurePatternReport, analyze_experiment, analyze_recent_payments
from razorpay_client.client import RazorpayReadClient
from razorpay_client.errors import RazorpayAPIError
from repository.actions import get_action_for_decision
from repository.audit import list_audit_trail
from repository.decisions import list_decisions_for_order
from repository.merchants import get_merchant, list_merchants
from repository.orders import get_order, list_orders_for_merchant
from repository.payment_attempts import get_payment_attempt, list_payment_attempts_for_order
from repository.payment_experiments import (
    get_experiment,
    list_experiment_orders_with_state,
)
from repository.recovery_batches import (
    list_batch_items_with_outcomes,
    list_batches_for_merchant,
    list_recent_batches,
)

from .schemas import (
    ActionSummary,
    CheckoutConfigResponse,
    CreateTestOrdersRequest,
    CreateTestOrdersResponse,
    CreatedOrderSummary,
    CustomerHistoryResponse,
    CustomerHistorySummary,
    ExperimentDetailResponse,
    ExperimentOrderState,
    BatchLedgerResponse,
    DiagnosisSummary,
    OutcomeBucketSummary,
    RecoveryBatchDetailResponse,
    RecoveryBatchItemSummary,
    RecoveryBatchListResponse,
    RecoveryBatchSummary,
    AuditEntrySummary,
    DecisionSummary,
    ErrorResponse,
    HealthResponse,
    MerchantListResponse,
    MerchantPaymentsResponse,
    MerchantSummary,
    MetricsResponse,
    OrderDetailResponse,
    OrderSummary,
    OrderTimelineResponse,
    OrderWithAttempts,
    PaymentAttemptSummary,
    PolicySummary,
    ReconcileResponse,
    SimulationDecisionSummary,
    SimulationPolicySummary,
    SimulationRequest,
    SimulationResponse,
)

app = FastAPI(
    title="Razorpay Decision Intelligence API",
    description="Minimal read/reconcile surface over the existing decision-intelligence pipeline.",
)

# Minimal, explicit local-development CORS configuration -- common
# Vite/Next dev-server origins only, not a wildcard. Adjust the origin
# list once the actual frontend dev port is known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_db() -> Iterator[psycopg.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _require_uuid(value: str, field_name: str) -> str:
    try:
        uuid_module.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} is not a valid identifier")
    return value


def _merchant_summary(row: dict[str, Any]) -> MerchantSummary:
    return MerchantSummary(id=str(row["id"]), name=row["name"], created_at=row["created_at"].isoformat())


def _payment_attempt_summary(row: dict[str, Any]) -> PaymentAttemptSummary:
    return PaymentAttemptSummary(
        id=row["id"], order_id=row["order_id"], status=row["status"], method=row["method"],
        captured=row["captured"], amount=row["amount"], error_source=row["error_source"],
        error_step=row["error_step"], error_reason=row["error_reason"], observed_at=row["observed_at"].isoformat(),
    )


def _order_summary(row: dict[str, Any]) -> OrderSummary:
    return OrderSummary(
        id=row["id"], merchant_id=str(row["merchant_id"]), amount=row["amount"], amount_paid=row["amount_paid"],
        amount_due=row["amount_due"], status=row["status"], attempts=row["attempts"], currency=row["currency"],
        observed_at=row["observed_at"].isoformat(),
    )


def _decision_summary(row: dict[str, Any]) -> DecisionSummary:
    return DecisionSummary(
        id=str(row["id"]), decision_type=row["decision_type"], confidence=float(row["confidence"]),
        reason_codes=row["reason_codes"], expected_impact=row["expected_impact"], model_version=row["model_version"],
        created_at=row["created_at"].isoformat(),
    )


def _policy_summary(policy_evaluation: dict[str, Any] | None) -> PolicySummary | None:
    if not policy_evaluation:
        return None
    return PolicySummary(
        policy_version=policy_evaluation.get("policy_version"),
        allowed=policy_evaluation.get("allowed"),
        authority_level_granted=policy_evaluation.get("authority_level_granted"),
        requires_approval=policy_evaluation.get("requires_approval"),
        reason_codes=policy_evaluation.get("reason_codes", []),
    )


def _action_summary(row: dict[str, Any]) -> ActionSummary:
    return ActionSummary(
        id=str(row["id"]), action_type=row["action_type"], status=row["status"],
        execution_reference=row["execution_reference"],
    )


def _audit_entry_summary(row: dict[str, Any]) -> AuditEntrySummary:
    return AuditEntrySummary(checkpoint=row["checkpoint"], snapshot=row["snapshot"], sequence_number=row["sequence_number"])


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/merchants", response_model=MerchantListResponse)
def merchants(conn: psycopg.Connection = Depends(get_db)) -> MerchantListResponse:
    rows = list_merchants(conn)
    return MerchantListResponse(merchants=[_merchant_summary(row) for row in rows])


@app.get(
    "/merchants/{merchant_id}/payments",
    response_model=MerchantPaymentsResponse,
    responses={404: {"model": ErrorResponse}},
)
def merchant_payments(merchant_id: str, conn: psycopg.Connection = Depends(get_db)) -> MerchantPaymentsResponse:
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    orders = list_orders_for_merchant(conn, merchant_id)
    order_details = [
        OrderWithAttempts(
            order=_order_summary(order),
            payment_attempts=[_payment_attempt_summary(a) for a in list_payment_attempts_for_order(conn, order["id"])],
        )
        for order in orders
    ]
    return MerchantPaymentsResponse(merchant_id=merchant_id, orders=order_details)


@app.get(
    "/merchants/{merchant_id}/metrics",
    response_model=MetricsResponse,
    responses={404: {"model": ErrorResponse}},
)
def merchant_metrics(merchant_id: str, conn: psycopg.Connection = Depends(get_db)) -> MetricsResponse:
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    return MetricsResponse(
        merchant_id=merchant_id,
        decision_type_distribution=decision_type_distribution(conn, merchant_id),
        policy_outcome_distribution=policy_outcome_distribution(conn, merchant_id),
        capture_terminal_status_distribution=capture_terminal_status_distribution(conn, merchant_id),
        escalation_metrics=escalation_metrics(conn, merchant_id),
        verification_read_attempt_distribution=verification_read_attempt_distribution(conn, merchant_id),
        verified_captured_amount=verified_captured_amount(conn, merchant_id),
        verification_resolution_timing=verification_resolution_timing(conn, merchant_id),
        retry_prompt_outcome_availability=retry_prompt_outcome_availability(conn, merchant_id),
    )


@app.get(
    "/orders/{order_id}",
    response_model=OrderDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
def order_detail(order_id: str, conn: psycopg.Connection = Depends(get_db)) -> OrderDetailResponse:
    order = get_order(conn, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    attempts = list_payment_attempts_for_order(conn, order_id)
    return OrderDetailResponse(order=_order_summary(order), payment_attempts=[_payment_attempt_summary(a) for a in attempts])


@app.get(
    "/orders/{order_id}/timeline",
    response_model=OrderTimelineResponse,
    responses={404: {"model": ErrorResponse}},
)
def order_timeline(order_id: str, conn: psycopg.Connection = Depends(get_db)) -> OrderTimelineResponse:
    order = get_order(conn, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    attempts = list_payment_attempts_for_order(conn, order_id)
    decisions = list_decisions_for_order(conn, order_id)

    response = OrderTimelineResponse(
        order=_order_summary(order), payment_attempts=[_payment_attempt_summary(a) for a in attempts],
    )
    if not decisions:
        return response

    latest_decision = decisions[-1]  # oldest-first; last is most recent
    response.decision = _decision_summary(latest_decision)

    action = get_action_for_decision(conn, latest_decision["id"])
    if action is not None:
        response.policy = _policy_summary(action["policy_evaluation"])
        response.action = _action_summary(action)
        response.verification = action["verification_result"]
        response.outcome = action["outcome"]

    audit_rows = list_audit_trail(conn, str(latest_decision["event_id"]), str(latest_decision["id"]))
    response.audit = [_audit_entry_summary(row) for row in audit_rows]
    return response


@app.post(
    "/merchants/{merchant_id}/orders/{order_id}/reconcile",
    response_model=ReconcileResponse,
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def reconcile(merchant_id: str, order_id: str, conn: psycopg.Connection = Depends(get_db)) -> ReconcileResponse:
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    try:
        read_client = RazorpayReadClient()
    except RuntimeError:
        raise HTTPException(status_code=500, detail="server is not configured with Razorpay credentials")

    try:
        result = run_reconciliation_pipeline(conn, read_client, merchant_id, order_id)
    except RazorpayAPIError:
        raise HTTPException(status_code=502, detail="Razorpay read failed for this order")
    except UnresolvedEventError:
        raise HTTPException(status_code=500, detail="reconciliation returned an event that could not be resolved")
    finally:
        read_client.close()

    return ReconcileResponse(order_id=result.order_id, new_event_count=result.new_event_count, events=result.events)


@app.post("/decision-lab/simulate", response_model=SimulationResponse)
def decision_lab_simulate(request: SimulationRequest) -> SimulationResponse:
    """Side-effect-free: runs the real RuleBasedEngine + policy engine
    against a synthetic, in-memory scenario (see pipeline/simulation.py).
    No database connection, no Razorpay call -- this endpoint works even
    without DATABASE_URL configured, and can never move money."""
    result = simulate_decision(
        amount=request.amount, status=request.status,
        auto_capture_limit=request.auto_capture_limit,
        approval_limit=request.approval_limit,
    )
    policy = (
        SimulationPolicySummary(
            policy_version=result.policy.policy_version,
            allowed=result.policy.allowed,
            authority_level_granted=result.policy.authority_level_granted,
            requires_approval=result.policy.requires_approval,
            reason_codes=result.policy.reason_codes,
        )
        if result.policy is not None
        else None
    )
    return SimulationResponse(
        input=request,
        decision=SimulationDecisionSummary(
            decision_type=result.decision.decision_type.value,
            confidence=result.decision.confidence,
            reason_codes=result.decision.reason_codes,
            model_version=result.decision.model_version,
        ),
        policy=policy,
        policy_skipped_reason=result.policy_skipped_reason,
    )



# ---------------------------------------------------------------------------
# Recovery batches -- READ ONLY, deliberately.
#
# There is no POST endpoint that runs a batch. Running one costs money twice
# over: it calls a language model once per at-risk payment, and on a real
# (non-synthetic) batch it can move money through Razorpay. Neither belongs
# behind a button on a public demo page that anyone can click repeatedly.
#
# Executing a batch is an operator action and lives in the command-line
# runner, not here. These endpoints only read what that produced.
# ---------------------------------------------------------------------------

def _diagnosis_from_context(context_snapshot: dict[str, Any] | None) -> DiagnosisSummary | None:
    """Recover the model's classification from the persisted AI_OUTPUT fields.

    Reads only fields banded AI_OUTPUT, so a DERIVED or RAW field can never be
    presented to a client as a model output. Returns None -- not a
    placeholder -- when the payment was never diagnosed, which is a real and
    common state (an authorized payment has no failure to explain, and a
    payment whose diagnosis failed was escalated instead).
    """
    if not context_snapshot:
        return None
    ai_fields = {
        f["field"]: f
        for f in context_snapshot.get("fields", [])
        if f.get("band") == "AI_OUTPUT"
    }
    if "diagnosed_root_cause" not in ai_fields:
        return None
    root = ai_fields["diagnosed_root_cause"]
    return DiagnosisSummary(
        root_cause=root["value"],
        failure_class=ai_fields["diagnosed_failure_class"]["value"],
        retry_advisable=ai_fields["diagnosed_retry_advisable"]["value"],
        confidence=root["confidence"],
        model_version=root["model_version"],
    )


@app.get(
    "/merchants/{merchant_id}/recovery-batches",
    response_model=RecoveryBatchListResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def merchant_recovery_batches(
    merchant_id: str, conn: psycopg.Connection = Depends(get_db)
) -> RecoveryBatchListResponse:
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    batches = [
        RecoveryBatchSummary(
            batch_id=str(row["id"]),
            merchant_id=str(row["merchant_id"]),
            source=row["source"],
            money_is_real=row["source"] == "razorpay_test_mode",
            detected_count=row["detected_count"],
            revenue_at_risk=row["revenue_at_risk"],
            created_at=row["created_at"],
        )
        for row in list_batches_for_merchant(conn, merchant_id)
    ]
    return RecoveryBatchListResponse(merchant_id=merchant_id, batches=batches)


@app.get(
    "/recovery-batches/{batch_id}",
    response_model=RecoveryBatchDetailResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def recovery_batch_detail(
    batch_id: str, conn: psycopg.Connection = Depends(get_db)
) -> RecoveryBatchDetailResponse:
    _require_uuid(batch_id, "batch_id")
    ledger = build_batch_ledger(conn, batch_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail="recovery batch not found")

    items = [
        RecoveryBatchItemSummary(
            payment_attempt_id=row["payment_attempt_id"],
            order_id=row["order_id"],
            amount_at_risk=row["amount_at_risk"],
            risk_reason_codes=row["risk_reason_codes"],
            error_reason=row["error_reason"],
            error_source=row["error_source"],
            method=row["method"],
            diagnosis=_diagnosis_from_context(row["context_snapshot"]),
            decision_id=str(row["decision_id"]) if row["decision_id"] else None,
            decision_type=row["decision_type"],
            decision_reason_codes=row["reason_codes"],
            action_id=str(row["action_id"]) if row["action_id"] else None,
            action_type=row["action_type"],
            action_status=row["action_status"],
        )
        for row in list_batch_items_with_outcomes(conn, batch_id)
    ]

    return RecoveryBatchDetailResponse(
        ledger=BatchLedgerResponse(
            batch_id=ledger.batch_id,
            merchant_id=ledger.merchant_id,
            source=ledger.source,
            money_is_real=ledger.money_is_real,
            detected_count=ledger.detected_count,
            revenue_at_risk=ledger.revenue_at_risk,
            at_risk_by_outcome=[
                OutcomeBucketSummary(category=b.category, count=b.count, amount=b.amount)
                for b in ledger.at_risk_by_outcome
            ],
            verified_recovered_amount=ledger.verified_recovered_amount,
            verified_recovered_count=ledger.verified_recovered_count,
            disposition_is_complete=ledger.disposition_is_complete,
        ),
        items=items,
    )


@app.get("/recovery-batches", response_model=RecoveryBatchListResponse)
def recent_recovery_batches(conn: psycopg.Connection = Depends(get_db)) -> RecoveryBatchListResponse:
    """Most recent batches across every merchant.

    The frontend uses this to find the batches worth displaying without
    hardcoding a demo merchant id -- a hardcoded identifier would break the
    moment the database is reseeded, and would make the page a fixture rather
    than a view over real data.
    """
    batches = [
        RecoveryBatchSummary(
            batch_id=str(row["id"]),
            merchant_id=str(row["merchant_id"]),
            merchant_name=row["merchant_name"],
            source=row["source"],
            money_is_real=row["source"] == "razorpay_test_mode",
            detected_count=row["detected_count"],
            revenue_at_risk=row["revenue_at_risk"],
            created_at=row["created_at"],
        )
        for row in list_recent_batches(conn, limit=10)
    ]
    # merchant_id is per-batch here rather than a single value for the whole
    # response, so the list-level field is deliberately left empty.
    return RecoveryBatchListResponse(merchant_id="", batches=batches)


# ---------------------------------------------------------------------------
# Guided problem journeys
# ---------------------------------------------------------------------------


@app.get("/checkout-config", response_model=CheckoutConfigResponse,
         responses={500: {"model": ErrorResponse}})
def checkout_config() -> CheckoutConfigResponse:
    """The publishable Razorpay key the browser needs to open Checkout.

    Two things this endpoint will not do. It never reads
    RAZORPAY_KEY_SECRET, so there is no code path on which a secret could
    be serialised into an HTTP response. And it refuses to serve a live
    key: this build is a Test Mode demonstration, and handing a browser a
    live key would let a visitor start real payments with real money. A
    non-test key is treated as a misconfiguration and the endpoint fails
    closed rather than degrading quietly.
    """
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    if not key_id:
        raise HTTPException(status_code=500, detail="server is not configured with a Razorpay key id")
    if not key_id.startswith("rzp_test_"):
        raise HTTPException(
            status_code=500,
            detail="refusing to serve a non-test Razorpay key to the browser",
        )
    return CheckoutConfigResponse(key_id=key_id, mode="test")


@app.post(
    "/merchants/{merchant_id}/test-orders",
    response_model=CreateTestOrdersResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
               409: {"model": ErrorResponse}, 502: {"model": ErrorResponse},
               500: {"model": ErrorResponse}},
)
def create_experiment_orders(
    merchant_id: str,
    request: CreateTestOrdersRequest,
    conn: psycopg.Connection = Depends(get_db),
) -> CreateTestOrdersResponse:
    """Creates real Razorpay Test Mode orders and freezes them as a cohort.

    Creating an order moves no money -- it is a request for payment that
    only a human completing Checkout can act on. The bounds (at most six,
    positive amount, known experiment kind) live in
    provisioning/test_orders.py rather than here, so any other caller gets
    the same limits.
    """
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    try:
        order_client = RazorpayOrderClient()
    except RuntimeError:
        raise HTTPException(status_code=500, detail="server is not configured with Razorpay credentials")

    try:
        result = create_test_orders(
            conn,
            order_client,
            merchant_id,
            kind=request.kind,
            count=request.count,
            amount=request.amount,
            currency=request.currency,
            label=request.label,
            experiment_id=request.experiment_id,
        )
    except CohortAlreadyInUse as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RazorpayAPIError:
        raise HTTPException(status_code=502, detail="Razorpay refused the order creation request")
    finally:
        order_client.close()

    return CreateTestOrdersResponse(
        experiment_id=result.experiment_id,
        merchant_id=result.merchant_id,
        kind=result.kind,
        orders=[
            CreatedOrderSummary(
                position=o.position, order_id=o.order_id, amount=o.amount,
                currency=o.currency, status=o.status,
            )
            for o in result.orders
        ],
    )


@app.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentDetailResponse,
    responses={404: {"model": ErrorResponse}},
)
def experiment_detail(
    experiment_id: str, conn: psycopg.Connection = Depends(get_db)
) -> ExperimentDetailResponse:
    """The cohort and whatever payment state has actually been observed.

    Orders nobody has paid come back with null payment fields rather than
    being omitted -- the cohort is the denominator and it does not shrink.
    """
    _require_uuid(experiment_id, "experiment_id")
    experiment = get_experiment(conn, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")

    rows = list_experiment_orders_with_state(conn, experiment_id)
    return ExperimentDetailResponse(
        experiment_id=str(experiment["id"]),
        merchant_id=str(experiment["merchant_id"]),
        kind=experiment["kind"],
        source=experiment["source"],
        label=experiment["label"],
        created_at=experiment["created_at"].isoformat(),
        orders=[
            ExperimentOrderState(
                position=row["position"],
                order_id=row["order_id"],
                amount=row["amount"],
                currency=row["currency"],
                order_status=row["order_status"],
                payment_attempt_id=row["payment_attempt_id"],
                payment_status=row["payment_status"],
                payment_captured=row["payment_captured"],
                payment_method=row["payment_method"],
                error_reason=row["error_reason"],
                error_step=row["error_step"],
                error_source=row["error_source"],
                payment_observed_at=(
                    row["payment_observed_at"].isoformat() if row["payment_observed_at"] else None
                ),
            )
            for row in rows
        ],
    )


@app.get(
    "/experiments/{experiment_id}/failure-pattern",
    response_model=FailurePatternReport,
    responses={404: {"model": ErrorResponse}},
)
def experiment_failure_pattern(
    experiment_id: str, conn: psycopg.Connection = Depends(get_db)
) -> FailurePatternReport:
    """Problem 03, over a frozen cohort: one payment failing, or many?"""
    _require_uuid(experiment_id, "experiment_id")
    if get_experiment(conn, experiment_id) is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return analyze_experiment(conn, experiment_id)


@app.get(
    "/merchants/{merchant_id}/failure-pattern",
    response_model=FailurePatternReport,
    responses={404: {"model": ErrorResponse}},
)
def merchant_failure_pattern(
    merchant_id: str, limit: int = 20, conn: psycopg.Connection = Depends(get_db)
) -> FailurePatternReport:
    """Problem 02, over this merchant's recent payments."""
    _require_uuid(merchant_id, "merchant_id")
    if get_merchant(conn, merchant_id) is None:
        raise HTTPException(status_code=404, detail="merchant not found")
    bounded_limit = max(1, min(limit, 200))
    return analyze_recent_payments(conn, merchant_id, limit=bounded_limit)


@app.get(
    "/payments/{payment_attempt_id}/customer-history",
    response_model=CustomerHistoryResponse,
    responses={404: {"model": ErrorResponse}},
)
def payment_customer_history(
    payment_attempt_id: str, conn: psycopg.Connection = Depends(get_db)
) -> CustomerHistoryResponse:
    """Problem 04: what this payer's previous payments with this merchant
    actually were.

    identity_available is reported separately from the counts so a caller
    can distinguish "this payer has no prior payments" from "this payment
    carries nothing to recognise a payer by". Collapsing those two into a
    zero would state a fact about a customer that was never observed.
    """
    attempt = get_payment_attempt(conn, payment_attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="payment not found")

    order = get_order(conn, attempt["order_id"])
    if order is None:
        raise HTTPException(status_code=404, detail="order not found for this payment")

    history = summarize_customer_history(
        conn,
        merchant_id=str(order["merchant_id"]),
        payment_attempt_id=payment_attempt_id,
        raw_reference=attempt["raw_reference"],
        as_of=attempt["observed_at"],
    )

    return CustomerHistoryResponse(
        payment_attempt_id=payment_attempt_id,
        identity_available=history is not None,
        history=CustomerHistorySummary(**history.model_dump()) if history else None,
    )


# Static frontend (apps/web/) -- plain HTML/CSS/JS, no build step. Mounted
# last, at "/", so it never shadows the API routes above: FastAPI/Starlette
# try explicit path operations first, in registration order, and only fall
# through to this mount for anything they didn't match. html=True serves
# index.html for "/" itself. This is wiring only -- no business logic.
_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="frontend")
