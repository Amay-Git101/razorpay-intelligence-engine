"""Domain contracts for the Phase 2 (Revision 2) approved design.

These types are the single source of truth for the shape of data crossing
module boundaries (ingestion -> context -> intelligence -> policy ->
action -> verification -> audit). No module should construct these
objects' underlying dicts by hand once this layer exists -- always go
through these types so validation (e.g. AI_OUTPUT provenance rules) is
enforced at construction time, not hoped for at each call site.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    CREATED = "created"
    ATTEMPTED = "attempted"  # DOCUMENTED only -- never independently observed
    PAID = "paid"


class PaymentAttemptStatus(str, Enum):
    CREATED = "created"  # DOCUMENTED only -- never independently observed
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"  # DOCUMENTED only -- untested


class EventType(str, Enum):
    ORDER_CREATED = "order.created"
    PAYMENT_ATTEMPT_FAILED = "payment.attempt.failed"
    PAYMENT_ATTEMPT_AUTHORIZED = "payment.attempt.authorized"
    PAYMENT_ATTEMPT_CAPTURED = "payment.attempt.captured"
    ORDER_PAID = "order.paid"
    PAYMENT_ATTEMPT_ANOMALY = "payment.attempt.anomaly"


class EventSource(str, Enum):
    RAZORPAY_API_POLL = "razorpay_api_poll"   # the only source used in v1
    RAZORPAY_WEBHOOK = "razorpay_webhook"     # reserved, unused in v1


class EntityType(str, Enum):
    ORDER = "order"
    PAYMENT = "payment"


class ProvenanceBand(str, Enum):
    RAW = "RAW"
    DERIVED = "DERIVED"
    AI_OUTPUT = "AI_OUTPUT"


class DecisionType(str, Enum):
    RECOMMEND_RETRY_PROMPT = "RECOMMEND_RETRY_PROMPT"
    RECOMMEND_CAPTURE = "RECOMMEND_CAPTURE"
    RECOMMEND_MERCHANT_ACTION = "RECOMMEND_MERCHANT_ACTION"
    NO_ACTION = "NO_ACTION"


class ActionType(str, Enum):
    CUSTOMER_RETRY_PROMPT = "CUSTOMER_RETRY_PROMPT"
    CAPTURE_PAYMENT = "CAPTURE_PAYMENT"


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    BLOCKED = "BLOCKED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    VERIFYING = "VERIFYING"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILED = "VERIFIED_FAILED"
    VERIFICATION_UNCERTAIN = "VERIFICATION_UNCERTAIN"
    ESCALATED = "ESCALATED"


class AuditCheckpoint(str, Enum):
    EVENT_INGESTED = "EVENT_INGESTED"
    RECONCILIATION_ANOMALY = "RECONCILIATION_ANOMALY"
    DECISION_CREATED = "DECISION_CREATED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    ACTION_BLOCKED = "ACTION_BLOCKED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    ACTION_AUTHORIZED = "ACTION_AUTHORIZED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"


# Phase 2 Revision 2, section G -- the only valid payment_attempt status
# transitions. Mirrors the database trigger in 0001_init.sql; kept here too
# so application code can check *before* attempting a write, not only find
# out via a caught database exception.
VALID_PAYMENT_ATTEMPT_TRANSITIONS: set[tuple[PaymentAttemptStatus, PaymentAttemptStatus]] = {
    (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.AUTHORIZED),
    (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.CAPTURED),
    (PaymentAttemptStatus.CREATED, PaymentAttemptStatus.FAILED),
    (PaymentAttemptStatus.AUTHORIZED, PaymentAttemptStatus.CAPTURED),
    (PaymentAttemptStatus.AUTHORIZED, PaymentAttemptStatus.FAILED),
    (PaymentAttemptStatus.CAPTURED, PaymentAttemptStatus.REFUNDED),
}


def is_valid_payment_attempt_transition(
    old_status: PaymentAttemptStatus, new_status: PaymentAttemptStatus
) -> bool:
    if old_status == new_status:
        return True
    return (old_status, new_status) in VALID_PAYMENT_ATTEMPT_TRANSITIONS


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class ProvenancedField(BaseModel):
    """One leaf value inside a ContextSnapshot or Expectation.

    A field tagged AI_OUTPUT must carry confidence and model_version --
    this is enforced here, at construction time, so a malformed AI_OUTPUT
    record can never reach the policy engine (Phase 2 Rev 2, invariant 8).
    """

    field: str
    value: Any
    band: ProvenanceBand
    source: str
    confidence: float | None = None
    model_version: str | None = None
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _validate_ai_output_metadata(self) -> "ProvenancedField":
        if self.band == ProvenanceBand.AI_OUTPUT:
            if self.confidence is None or self.model_version is None:
                raise ValueError(
                    f"AI_OUTPUT field '{self.field}' is missing confidence and/or "
                    "model_version -- malformed AI_OUTPUT is rejected before it can "
                    "reach the policy engine"
                )
            if not (0.0 <= self.confidence <= 1.0):
                raise ValueError(
                    f"AI_OUTPUT field '{self.field}' has out-of-range confidence: "
                    f"{self.confidence}"
                )
        return self


class ContextSnapshot(BaseModel):
    order_id: str
    payment_attempt_id: str | None = None
    fields: list[ProvenancedField]
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Expectation(BaseModel):
    bucket_key: str
    expected_recovery_rate: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    source: str


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

class DecisionOutput(BaseModel):
    """The contract every IntelligenceEngine implementation must produce.

    RuleBasedEngine (v1) and any future MLDecisionEngine both return
    exactly this shape -- downstream modules (policy/action/audit) never
    branch on which implementation produced it.
    """

    decision_type: DecisionType
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]
    expected_impact: dict[str, Any] = Field(default_factory=dict)
    model_version: str


class Decision(BaseModel):
    """The full persisted record (decisions table)."""

    merchant_id: str
    order_id: str
    payment_attempt_id: str | None = None
    event_id: str
    context_snapshot: ContextSnapshot
    expectation: Expectation
    decision_type: DecisionType
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]
    expected_impact: dict[str, Any] = Field(default_factory=dict)
    model_version: str


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class PolicyInput(BaseModel):
    merchant_id: str
    decision_type: DecisionType
    action_type: ActionType
    amount: int = Field(ge=0)
    moves_money: bool
    merchant_policy_config: dict[str, Any]
    current_order_action_count: int = 0


class PolicyRuleResult(BaseModel):
    rule_id: str
    matched: bool
    outcome: str | None = None


class PolicyEvaluation(BaseModel):
    policy_version: str
    rules_evaluated: list[PolicyRuleResult]
    allowed: bool
    authority_level_granted: str
    requires_approval: bool
    reason_codes: list[str]


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def compute_idempotency_key(
    merchant_id: str, order_id: str, payment_attempt_id: str, action_type: ActionType
) -> str:
    """Operation-level idempotency identity.

    Deliberately excludes decision_id (Phase 2 Rev 2, point 2): two
    separate Decisions recommending the same real-world operation must
    collide on the same key, not produce two independent ones.
    """
    raw = f"{merchant_id}|{order_id}|{payment_attempt_id}|{action_type.value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Action(BaseModel):
    decision_id: str
    idempotency_key: str
    action_type: ActionType
    policy_evaluation: PolicyEvaluation
    status: ActionStatus = ActionStatus.PROPOSED
    execution_reference: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class AuditEntry(BaseModel):
    checkpoint: AuditCheckpoint
    snapshot: dict[str, Any]
    event_id: str | None = None
    decision_id: str | None = None
    action_id: str | None = None
