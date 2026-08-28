"""evaluate_policy(): the single deterministic entry point. Same
PolicyInput always produces the same PolicyEvaluation. Independent of
the LLM/RuleBasedEngine entirely -- takes only the already-built
PolicyInput.
"""

from __future__ import annotations

from domain.contracts import PolicyEvaluation, PolicyInput, PolicyRuleResult

from .rules import (
    POLICY_VERSION,
    PolicyConfigError,
    check_approval_band,
    check_auto_allow_under_limit,
    check_hard_limit,
    check_no_money_movement,
)


def evaluate_policy(policy_input: PolicyInput) -> PolicyEvaluation:
    rules_evaluated: list[PolicyRuleResult] = []

    no_money_result, no_money_matched = check_no_money_movement(policy_input)
    rules_evaluated.append(no_money_result)
    if no_money_matched:
        return PolicyEvaluation(
            policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
            allowed=True, authority_level_granted="RECOMMEND", requires_approval=False,
            reason_codes=["NO_MONEY_MOVEMENT"],
        )

    try:
        auto_allow_result, auto_allow_matched = check_auto_allow_under_limit(policy_input)
        rules_evaluated.append(auto_allow_result)
        if auto_allow_matched:
            return PolicyEvaluation(
                policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
                allowed=True, authority_level_granted="AUTOMATIC", requires_approval=False,
                reason_codes=["WITHIN_AUTO_ALLOW_LIMIT"],
            )

        approval_result, approval_matched = check_approval_band(policy_input)
        rules_evaluated.append(approval_result)
        if approval_matched:
            return PolicyEvaluation(
                policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
                allowed=True, authority_level_granted="PREPARE", requires_approval=True,
                reason_codes=["WITHIN_APPROVAL_BAND"],
            )

        hard_limit_result, hard_limit_matched = check_hard_limit(policy_input)
        rules_evaluated.append(hard_limit_result)
        if hard_limit_matched:
            return PolicyEvaluation(
                policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
                allowed=False, authority_level_granted="FORBIDDEN", requires_approval=False,
                reason_codes=["AMOUNT_EXCEEDS_HARD_LIMIT"],
            )
    except PolicyConfigError:
        rules_evaluated.append(
            PolicyRuleResult(rule_id="POLICY_CONFIG_VALIDATION", matched=True, outcome="BLOCK")
        )
        return PolicyEvaluation(
            policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
            allowed=False, authority_level_granted="FORBIDDEN", requires_approval=False,
            reason_codes=["POLICY_CONFIG_INVALID"],
        )

    # Unreachable given valid, consistent config (the three bands are
    # exhaustive) -- but never silently allow if somehow nothing matched.
    rules_evaluated.append(PolicyRuleResult(rule_id="NO_RULE_MATCHED_FALLBACK", matched=True, outcome="BLOCK"))
    return PolicyEvaluation(
        policy_version=POLICY_VERSION, rules_evaluated=rules_evaluated,
        allowed=False, authority_level_granted="FORBIDDEN", requires_approval=False,
        reason_codes=["NO_POLICY_RULE_MATCHED"],
    )
