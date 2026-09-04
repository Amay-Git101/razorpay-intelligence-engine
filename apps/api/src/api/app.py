"""Minimal HTTP API surface for the frontend.

    GET  /health
    GET  /merchants
    GET  /merchants/{merchant_id}/payments
    GET  /merchants/{merchant_id}/metrics
    GET  /orders/{order_id}
    GET  /orders/{order_id}/timeline
    POST /merchants/{merchant_id}/orders/{order_id}/reconcile
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

import uuid as uuid_module
from pathlib import Path
from typing import Any, Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db.connection import get_connection
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
from pipeline.orchestration import UnresolvedEventError, run_reconciliation_pipeline
from pipeline.simulation import simulate_decision
from razorpay_client.client import RazorpayReadClient
from razorpay_client.errors import RazorpayAPIError
from repository.actions import get_action_for_decision
from repository.audit import list_audit_trail
from repository.decisions import list_decisions_for_order
from repository.merchants import get_merchant, list_merchants
from repository.orders import get_order, list_orders_for_merchant
from repository.payment_attempts import list_payment_attempts_for_order

from .schemas import (
    ActionSummary,
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


# Static frontend (apps/web/) -- plain HTML/CSS/JS, no build step. Mounted
# last, at "/", so it never shadows the API routes above: FastAPI/Starlette
# try explicit path operations first, in registration order, and only fall
# through to this mount for anything they didn't match. html=True serves
# index.html for "/" itself. This is wiring only -- no business logic.
_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="frontend")
