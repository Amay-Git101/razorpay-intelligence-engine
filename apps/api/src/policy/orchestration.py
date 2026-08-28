"""Fetches a persisted Decision, builds PolicyInput, runs evaluate_policy,
and writes a POLICY_EVALUATED audit entry referencing decision_id only --
action_id stays NULL, since Actions don't exist yet (Action gate is
next) and this gate does not create one merely for audit linkage.

NO_ACTION decisions are explicitly not policy-gated: they are not action
candidates, so evaluate_decision() raises rather than manufacturing a
policy result for something nothing will ever execute. DECISION_CREATED
remains the terminal audit record for those.

Amount is read from decision.expected_impact["revenue_at_stake"] -- the
same field RuleBasedEngine already populates for RECOMMEND_RETRY_PROMPT.
A test-constructed RECOMMEND_CAPTURE decision (RuleBasedEngine doesn't
produce this decision_type yet, per gate scope) must populate the same
field for evaluate_decision to read an amount from it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from domain.contracts import ActionType, DecisionType, PolicyEvaluation, PolicyInput
from repository.audit import insert_audit_entry
from repository.decisions import get_decision
from repository.merchants import get_merchant

from .engine import evaluate_policy

# decision_type -> (action_type, moves_money). NO_ACTION is deliberately
# absent -- see NotPolicyGated below.
_DECISION_TYPE_TO_ACTION: dict[str, tuple[str, bool]] = {
    "RECOMMEND_RETRY_PROMPT": ("CUSTOMER_RETRY_PROMPT", False),
    "RECOMMEND_CAPTURE": ("CAPTURE_PAYMENT", True),
}


class NotPolicyGated(Exception):
    """Raised when asked to policy-gate a decision_type that isn't an
    action candidate (NO_ACTION), or one this gate doesn't support."""


def evaluate_decision(conn: psycopg.Connection, decision_id: UUID | str) -> PolicyEvaluation:
    decision = get_decision(conn, decision_id)
    if decision is None:
        raise ValueError(f"no decision found with id {decision_id}")

    decision_type = decision["decision_type"]
    if decision_type == "NO_ACTION":
        raise NotPolicyGated(
            "NO_ACTION decisions are not policy-gated -- DECISION_CREATED is "
            "the terminal audit record for these, not POLICY_EVALUATED"
        )
    if decision_type not in _DECISION_TYPE_TO_ACTION:
        raise NotPolicyGated(f"unsupported decision_type for policy gating: {decision_type}")

    action_type, moves_money = _DECISION_TYPE_TO_ACTION[decision_type]

    merchant = get_merchant(conn, decision["merchant_id"])
    expected_impact: dict[str, Any] = decision["expected_impact"] or {}
    amount = expected_impact.get("revenue_at_stake", 0)

    policy_input = PolicyInput(
        merchant_id=str(decision["merchant_id"]),
        decision_type=DecisionType(decision_type),
        action_type=ActionType(action_type),
        amount=amount,
        moves_money=moves_money,
        merchant_policy_config=merchant["policy_config"] if merchant else {},
    )

    evaluation = evaluate_policy(policy_input)

    insert_audit_entry(
        conn,
        "POLICY_EVALUATED",
        {
            "allowed": evaluation.allowed,
            "authority_level_granted": evaluation.authority_level_granted,
            "requires_approval": evaluation.requires_approval,
            "reason_codes": evaluation.reason_codes,
        },
        decision_id=str(decision_id),
    )

    return evaluation
