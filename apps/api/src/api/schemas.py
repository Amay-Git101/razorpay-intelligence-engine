"""Response models for the HTTP API.

These are presentation-boundary schemas only -- they re-shape existing
domain/repository/observability data for JSON, they never introduce a
new domain state. A field that has no value yet (a decision that never
happened, an action that was never proposed, verification that never
ran) is represented as an absent/None field, never a fabricated status.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from observability.metrics import (
    CaptureTerminalStatusDistribution,
    DecisionTypeDistribution,
    EscalationMetrics,
    PolicyOutcomeDistribution,
    RetryPromptOutcomeAvailability,
    VerificationReadAttemptDistribution,
    VerificationResolutionTiming,
    VerifiedCapturedAmount,
)
from pipeline.orchestration import EventProcessingResult


class MerchantSummary(BaseModel):
    id: str
    name: str
    created_at: str


class MerchantListResponse(BaseModel):
    merchants: list[MerchantSummary]


class PaymentAttemptSummary(BaseModel):
    id: str
    order_id: str
    status: str
    method: str | None = None
    captured: bool
    amount: int
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    observed_at: str


class OrderSummary(BaseModel):
    id: str
    merchant_id: str
    amount: int
    amount_paid: int
    amount_due: int
    status: str
    attempts: int
    currency: str
    observed_at: str


class OrderWithAttempts(BaseModel):
    order: OrderSummary
    payment_attempts: list[PaymentAttemptSummary]


class MerchantPaymentsResponse(BaseModel):
    merchant_id: str
    orders: list[OrderWithAttempts]


class OrderDetailResponse(BaseModel):
    order: OrderSummary
    payment_attempts: list[PaymentAttemptSummary]


class DecisionSummary(BaseModel):
    id: str
    decision_type: str
    confidence: float
    reason_codes: list[str]
    expected_impact: dict[str, Any]
    model_version: str
    created_at: str


class PolicySummary(BaseModel):
    """Directly re-presents actions.policy_evaluation -- not a new
    source of truth, just a friendlier top-level key for it."""

    policy_version: str | None = None
    allowed: bool | None = None
    authority_level_granted: str | None = None
    requires_approval: bool | None = None
    reason_codes: list[str] = []


class ActionSummary(BaseModel):
    id: str
    action_type: str
    status: str
    execution_reference: dict[str, Any] | None = None


class AuditEntrySummary(BaseModel):
    checkpoint: str
    snapshot: dict[str, Any]
    sequence_number: int


class OrderTimelineResponse(BaseModel):
    """Reconciliation -> Context/Decision -> Policy -> Action ->
    Verification -> Outcome, for the most recent Decision on this
    order. A stage that never happened is None, never fabricated."""

    order: OrderSummary
    payment_attempts: list[PaymentAttemptSummary]
    decision: DecisionSummary | None = None
    policy: PolicySummary | None = None
    action: ActionSummary | None = None
    verification: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    audit: list[AuditEntrySummary] = []


class ReconcileResponse(BaseModel):
    """Directly wraps pipeline.orchestration.PipelineRunResult -- the
    same structured result the project's CLI tooling prints."""

    order_id: str
    new_event_count: int
    events: list[EventProcessingResult]


class MetricsResponse(BaseModel):
    """Direct serialization of the existing observability functions'
    own report models -- no new metric is computed here. Each nested
    report carries its own caveat/scope_note field explaining what it
    does and does not mean; the frontend should surface those, not
    strip them."""

    merchant_id: str
    decision_type_distribution: DecisionTypeDistribution
    policy_outcome_distribution: PolicyOutcomeDistribution
    capture_terminal_status_distribution: CaptureTerminalStatusDistribution
    escalation_metrics: EscalationMetrics
    verification_read_attempt_distribution: VerificationReadAttemptDistribution
    verified_captured_amount: VerifiedCapturedAmount
    verification_resolution_timing: VerificationResolutionTiming
    retry_prompt_outcome_availability: RetryPromptOutcomeAvailability


class HealthResponse(BaseModel):
    status: str


class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Decision lab (synthetic, side-effect-free scenario simulation)
# ---------------------------------------------------------------------------


class SimulationRequest(BaseModel):
    """A hypothetical payment state, never a real Razorpay identifier or
    a real database row -- see pipeline/simulation.py for the safety
    boundary this enforces (no DB, no Razorpay, no write capability)."""

    amount: int = Field(ge=0)
    status: str
    auto_capture_limit: int = Field(ge=0)
    approval_limit: int = Field(ge=0)


class SimulationDecisionSummary(BaseModel):
    decision_type: str
    confidence: float
    reason_codes: list[str]
    model_version: str


class SimulationPolicySummary(BaseModel):
    policy_version: str | None = None
    allowed: bool | None = None
    authority_level_granted: str | None = None
    requires_approval: bool | None = None
    reason_codes: list[str] = []


class SimulationResponse(BaseModel):
    input: SimulationRequest
    decision: SimulationDecisionSummary
    policy: SimulationPolicySummary | None = None
    policy_skipped_reason: str | None = None
