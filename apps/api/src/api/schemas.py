"""Response models for the HTTP API.

These are presentation-boundary schemas only -- they re-shape existing
domain/repository/observability data for JSON, they never introduce a
new domain state. A field that has no value yet (a decision that never
happened, an action that was never proposed, verification that never
ran) is represented as an absent/None field, never a fabricated status.
"""

from __future__ import annotations

from datetime import datetime
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


# ---------------------------------------------------------------------------
# Recovery batches
# ---------------------------------------------------------------------------

class OutcomeBucketSummary(BaseModel):
    category: str
    count: int
    amount: int


class RecoveryBatchSummary(BaseModel):
    batch_id: str
    merchant_id: str
    merchant_name: str | None = None
    source: str
    # Denormalised from `source` at the API boundary so a client cannot
    # render this batch without the fact. Every response that carries an
    # amount also carries whether that amount is real money.
    money_is_real: bool
    detected_count: int
    revenue_at_risk: int
    created_at: datetime


class RecoveryBatchListResponse(BaseModel):
    merchant_id: str
    batches: list[RecoveryBatchSummary]


class BatchLedgerResponse(BaseModel):
    batch_id: str
    merchant_id: str
    source: str
    money_is_real: bool
    detected_count: int
    revenue_at_risk: int
    at_risk_by_outcome: list[OutcomeBucketSummary]
    verified_recovered_amount: int
    verified_recovered_count: int
    # False means the outcome buckets no longer sum to revenue_at_risk. It is
    # surfaced rather than hidden so a client can refuse to draw a partition
    # that does not add up, instead of silently drawing a wrong one.
    disposition_is_complete: bool


class DiagnosisSummary(BaseModel):
    """The model's classification, read back out of the persisted context
    snapshot's AI_OUTPUT fields. Confidence and model_version are always
    present -- ProvenancedField refuses to construct an AI_OUTPUT field
    without them, so a diagnosis cannot reach this response stripped of its
    provenance."""

    root_cause: str
    failure_class: str
    retry_advisable: bool
    confidence: float
    model_version: str


class RecoveryBatchItemSummary(BaseModel):
    payment_attempt_id: str
    order_id: str
    amount_at_risk: int
    risk_reason_codes: list[str]
    error_reason: str | None = None
    error_source: str | None = None
    method: str | None = None
    diagnosis: DiagnosisSummary | None = None
    decision_id: str | None = None
    decision_type: str | None = None
    decision_reason_codes: list[str] | None = None
    action_id: str | None = None
    action_type: str | None = None
    action_status: str | None = None


class RecoveryBatchDetailResponse(BaseModel):
    ledger: BatchLedgerResponse
    items: list[RecoveryBatchItemSummary]


# ---------------------------------------------------------------------------
# Guided problem journeys
# ---------------------------------------------------------------------------


class CheckoutConfigResponse(BaseModel):
    """The only Razorpay configuration the browser is ever given.

    key_id is Razorpay's PUBLISHABLE identifier -- it is designed to be
    embedded in a page, it is what Checkout requires, and it cannot
    authenticate a server-side API call on its own. The secret is never
    part of this contract: there is no field here it could travel in.
    """

    key_id: str
    mode: str


class CreateTestOrdersRequest(BaseModel):
    kind: str
    count: int = Field(ge=1, le=6)
    amount: int = Field(gt=0)
    currency: str = "INR"
    label: str | None = None


class CreatedOrderSummary(BaseModel):
    position: int
    order_id: str
    amount: int
    currency: str
    status: str


class CreateTestOrdersResponse(BaseModel):
    experiment_id: str
    merchant_id: str
    kind: str
    orders: list[CreatedOrderSummary]


class ExperimentOrderState(BaseModel):
    """One cohort order and whatever has actually been observed for it.

    Every payment field is optional because an order nobody has paid yet
    genuinely has no payment state. It is reported as absent rather than
    defaulted to a status that was never observed.
    """

    position: int
    order_id: str
    amount: int
    currency: str
    order_status: str
    payment_attempt_id: str | None = None
    payment_status: str | None = None
    payment_captured: bool | None = None
    payment_method: str | None = None
    error_reason: str | None = None
    error_step: str | None = None
    error_source: str | None = None
    payment_observed_at: str | None = None


class ExperimentDetailResponse(BaseModel):
    experiment_id: str
    merchant_id: str
    kind: str
    source: str
    label: str | None = None
    created_at: str
    orders: list[ExperimentOrderState]


class CustomerHistoryResponse(BaseModel):
    """Prior payment outcomes for the payer of one payment.

    history is null when the payment carries no identity to recognise the
    payer by -- which is the honest answer for a synthetic row, and is
    never presented as "this customer has no history".
    """

    payment_attempt_id: str
    identity_available: bool
    history: CustomerHistorySummary | None = None


class CustomerHistorySummary(BaseModel):
    identity_kind: str
    identity_fingerprint: str
    lookback_days: int
    prior_payment_count: int
    prior_captured_count: int
    prior_authorized_count: int
    prior_failed_count: int
    distinct_prior_orders: int
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    prior_failure_reasons: dict[str, int] = {}


CustomerHistoryResponse.model_rebuild()
