"""Policy engine tests. Pure Python, no DB -- hand-built PolicyInput
objects, no Decision/merchant persistence involved."""

from __future__ import annotations

import pytest

from domain.contracts import ActionType, DecisionType, PolicyInput
from policy.engine import evaluate_policy
from policy.rules import PolicyConfigError, check_no_money_movement

MERCHANT_POLICY_CONFIG = {"max_auto_capture_amount": 20000, "approval_band_upper": 100000}


def _capture_input(amount: int, config: dict | None = None) -> PolicyInput:
    return PolicyInput(
        merchant_id="merchant_x",
        decision_type=DecisionType.RECOMMEND_CAPTURE,
        action_type=ActionType.CAPTURE_PAYMENT,
        amount=amount,
        moves_money=True,
        merchant_policy_config=config if config is not None else MERCHANT_POLICY_CONFIG,
    )


def _retry_prompt_input(amount: int = 0) -> PolicyInput:
    return PolicyInput(
        merchant_id="merchant_x",
        decision_type=DecisionType.RECOMMEND_RETRY_PROMPT,
        action_type=ActionType.CUSTOMER_RETRY_PROMPT,
        amount=amount,
        moves_money=False,
        merchant_policy_config={},  # deliberately empty -- must not matter
    )


# ---------------------------------------------------------------------------
# No-money-movement: distinct, terminal, first rule -- never combined
# with amount thresholds via `or`
# ---------------------------------------------------------------------------

def test_no_money_movement_always_allowed_regardless_of_amount_or_missing_config():
    for amount in (0, 1, 999_999_999):
        evaluation = evaluate_policy(_retry_prompt_input(amount=amount))
        assert evaluation.allowed is True
        assert evaluation.requires_approval is False
        assert evaluation.reason_codes == ["NO_MONEY_MOVEMENT"]
        # only ONE rule is ever evaluated for a no-money-movement decision --
        # amount thresholds are never consulted at all
        assert [r.rule_id for r in evaluation.rules_evaluated] == ["NO_MONEY_MOVEMENT_AUTO_ALLOW"]


def test_no_money_movement_rule_is_a_standalone_check():
    result, matched = check_no_money_movement(_retry_prompt_input())
    assert matched is True
    assert result.outcome == "ALLOW"


# ---------------------------------------------------------------------------
# Three exact bands for money-moving decisions
# ---------------------------------------------------------------------------

def test_amount_at_auto_allow_limit_is_allowed_no_approval():
    evaluation = evaluate_policy(_capture_input(20000))  # exactly max_auto_capture_amount
    assert evaluation.allowed is True
    assert evaluation.requires_approval is False
    assert "WITHIN_AUTO_ALLOW_LIMIT" in evaluation.reason_codes


def test_amount_one_above_auto_allow_limit_requires_approval():
    evaluation = evaluate_policy(_capture_input(20001))
    assert evaluation.allowed is True
    assert evaluation.requires_approval is True
    assert "WITHIN_APPROVAL_BAND" in evaluation.reason_codes


def test_amount_at_approval_band_upper_requires_approval():
    evaluation = evaluate_policy(_capture_input(100000))  # exactly approval_band_upper
    assert evaluation.allowed is True
    assert evaluation.requires_approval is True
    assert "WITHIN_APPROVAL_BAND" in evaluation.reason_codes


def test_amount_one_above_approval_band_upper_is_blocked():
    evaluation = evaluate_policy(_capture_input(100001))
    assert evaluation.allowed is False
    assert "AMOUNT_EXCEEDS_HARD_LIMIT" in evaluation.reason_codes


def test_amount_well_under_limit_is_allowed_no_approval():
    evaluation = evaluate_policy(_capture_input(1))
    assert evaluation.allowed is True
    assert evaluation.requires_approval is False


def test_amount_far_above_limit_is_blocked():
    evaluation = evaluate_policy(_capture_input(10_000_000))
    assert evaluation.allowed is False


# ---------------------------------------------------------------------------
# rules_evaluated always records every rule checked, not just the winner
# ---------------------------------------------------------------------------

def test_rules_evaluated_lists_all_checked_rules_for_auto_allow():
    evaluation = evaluate_policy(_capture_input(20000))
    rule_ids = [r.rule_id for r in evaluation.rules_evaluated]
    assert rule_ids == ["NO_MONEY_MOVEMENT_AUTO_ALLOW", "AUTO_ALLOW_UNDER_LIMIT"]


def test_rules_evaluated_lists_all_checked_rules_for_approval_band():
    evaluation = evaluate_policy(_capture_input(50000))
    rule_ids = [r.rule_id for r in evaluation.rules_evaluated]
    assert rule_ids == ["NO_MONEY_MOVEMENT_AUTO_ALLOW", "AUTO_ALLOW_UNDER_LIMIT", "APPROVAL_BAND_CHECK"]


def test_rules_evaluated_lists_all_checked_rules_for_hard_block():
    evaluation = evaluate_policy(_capture_input(200000))
    rule_ids = [r.rule_id for r in evaluation.rules_evaluated]
    assert rule_ids == [
        "NO_MONEY_MOVEMENT_AUTO_ALLOW", "AUTO_ALLOW_UNDER_LIMIT", "APPROVAL_BAND_CHECK", "HARD_LIMIT_CHECK",
    ]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_evaluate_policy_is_deterministic():
    policy_input = _capture_input(50000)
    first = evaluate_policy(policy_input)
    second = evaluate_policy(policy_input)
    assert first == second


# ---------------------------------------------------------------------------
# Fail closed on invalid/missing config -- never silently permissive
# ---------------------------------------------------------------------------

def test_missing_max_auto_capture_amount_blocks():
    evaluation = evaluate_policy(_capture_input(100, config={"approval_band_upper": 100000}))
    assert evaluation.allowed is False
    assert "POLICY_CONFIG_INVALID" in evaluation.reason_codes


def test_missing_approval_band_upper_blocks():
    # amount chosen so it clears auto-allow and reaches the approval-band
    # check, which is where approval_band_upper is required
    evaluation = evaluate_policy(_capture_input(50000, config={"max_auto_capture_amount": 20000}))
    assert evaluation.allowed is False
    assert "POLICY_CONFIG_INVALID" in evaluation.reason_codes


def test_negative_limit_blocks():
    evaluation = evaluate_policy(
        _capture_input(100, config={"max_auto_capture_amount": -1, "approval_band_upper": 100000})
    )
    assert evaluation.allowed is False


def test_non_numeric_limit_blocks():
    evaluation = evaluate_policy(
        _capture_input(100, config={"max_auto_capture_amount": "not-a-number", "approval_band_upper": 100000})
    )
    assert evaluation.allowed is False


def test_inconsistent_config_upper_below_lower_blocks():
    # amount must exceed max_auto_capture_amount so evaluation actually
    # reaches the approval-band check, where the inconsistency is caught
    evaluation = evaluate_policy(
        _capture_input(150000, config={"max_auto_capture_amount": 100000, "approval_band_upper": 20000})
    )
    assert evaluation.allowed is False
    assert "POLICY_CONFIG_INVALID" in evaluation.reason_codes


def test_empty_config_never_becomes_permissive():
    evaluation = evaluate_policy(_capture_input(1, config={}))
    assert evaluation.allowed is False


def test_policy_config_error_raised_directly_by_rule_functions():
    from policy.rules import check_auto_allow_under_limit
    with pytest.raises(PolicyConfigError):
        check_auto_allow_under_limit(_capture_input(100, config={}))
