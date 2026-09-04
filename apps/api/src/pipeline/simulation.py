"""Pure, side-effect-free decision/policy simulation for the frontend's
"Try a scenario" lab.

Reuses the REAL RuleBasedEngine and evaluate_policy() -- this module adds
no business rules of its own. It assembles the same ContextSnapshot/
PolicyInput shapes reconciliation and policy/orchestration.py build from
live data, from user-supplied synthetic inputs instead, and imports the
decision_type -> (action_type, moves_money) mapping from
policy.orchestration rather than re-typing it, so the simulation can
never drift from the real mapping.

SAFETY: this module touches no database and no Razorpay client -- it
cannot read or write anything, anywhere. Action and Verification are
never invoked here: a simulated RECOMMEND_CAPTURE decision that Policy
ALLOWs is reported as "would be allowed to act", never actually
executed. No money can move through this path -- this file imports no
Razorpay client of any kind (read or write) and no database driver.
"""

from __future__ import annotations

from pydantic import BaseModel

from domain.contracts import (
    ActionType,
    ContextSnapshot,
    DecisionOutput,
    DecisionType,
    Expectation,
    PolicyEvaluation,
    PolicyInput,
    ProvenanceBand,
    ProvenancedField,
)
from intelligence.rule_based import RuleBasedEngine
from policy.engine import evaluate_policy
from policy.orchestration import _DECISION_TYPE_TO_ACTION

SIMULATION_BUCKET_KEY = "simulation"
SIMULATION_ORDER_ID = "simulation"
SIMULATION_PAYMENT_ATTEMPT_ID = "simulation_payment"
SIMULATION_MERCHANT_ID = "simulation"


class SimulationResult(BaseModel):
    """Never persisted, never audited -- this is a what-if computation
    only. A field that a real pipeline run would populate but this one
    deliberately skips (e.g. policy, when the decision isn't
    policy-gated) stays None with policy_skipped_reason explaining why,
    the same "never fabricate a stage" discipline used everywhere else
    in this project."""

    context: ContextSnapshot
    decision: DecisionOutput
    policy: PolicyEvaluation | None = None
    policy_skipped_reason: str | None = None


def simulate_decision(
    amount: int, status: str, auto_capture_limit: int, approval_limit: int,
) -> SimulationResult:
    """Runs the real decision + policy engines against a synthetic,
    in-memory payment context. order_id/payment_attempt_id/merchant_id
    are fixed synthetic placeholders -- never real Razorpay or database
    identifiers -- so a simulation result can never be mistaken for a
    real order by anything reading it downstream.

    auto_capture_limit/approval_limit are mapped onto the real
    merchant_policy_config key names below -- the only place those key
    names need to appear, since this function is the one place that
    actually has to match policy/rules.py's expected config shape."""
    context = ContextSnapshot(
        order_id=SIMULATION_ORDER_ID,
        payment_attempt_id=SIMULATION_PAYMENT_ATTEMPT_ID,
        fields=[
            ProvenancedField(field="amount", value=amount, band=ProvenanceBand.RAW, source="simulation_input"),
            ProvenancedField(field="status", value=status, band=ProvenanceBand.RAW, source="simulation_input"),
        ],
    )
    expectation = Expectation(
        bucket_key=SIMULATION_BUCKET_KEY, expected_recovery_rate=0.5, sample_size=0, source="simulation",
    )

    decision = RuleBasedEngine().evaluate(context, expectation)

    if decision.decision_type.value not in _DECISION_TYPE_TO_ACTION:
        return SimulationResult(
            context=context, decision=decision, policy=None,
            policy_skipped_reason=f"{decision.decision_type.value} is not policy-gated",
        )

    action_type_str, moves_money = _DECISION_TYPE_TO_ACTION[decision.decision_type.value]
    policy_input = PolicyInput(
        merchant_id=SIMULATION_MERCHANT_ID,
        decision_type=decision.decision_type,
        action_type=ActionType(action_type_str),
        amount=amount,
        moves_money=moves_money,
        merchant_policy_config={
            "max_auto_capture_amount": auto_capture_limit,
            "approval_band_upper": approval_limit,
        },
    )
    policy = evaluate_policy(policy_input)
    return SimulationResult(context=context, decision=decision, policy=policy)
