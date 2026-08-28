"""Deterministic, independent-of-the-LLM policy rules.

Each check function takes a PolicyInput and returns
(PolicyRuleResult, matched: bool). engine.py evaluates them in a fixed
order and records every one checked in rules_evaluated, not just the
winning rule.

Locked from gate review:
  - The no-money-movement check is a distinct, terminal, FIRST rule --
    never combined with the amount thresholds via `or`.
  - Money-moving decisions go through exactly three mutually exclusive
    bands: auto-allow (<= max_auto_capture_amount), approval-required
    (max_auto_capture_amount < amount <= approval_band_upper), hard
    block (> approval_band_upper).
  - Missing/invalid monetary policy config raises PolicyConfigError --
    fails closed, never silently permissive.
"""

from __future__ import annotations

from typing import Any

from domain.contracts import PolicyInput, PolicyRuleResult

POLICY_VERSION = "policy_v1"


class PolicyConfigError(Exception):
    """Raised when merchant_policy_config is missing a required key, has
    an invalid value, or is internally inconsistent. Callers must treat
    this as grounds to block, never to fall back to a permissive
    default."""


def check_no_money_movement(policy_input: PolicyInput) -> tuple[PolicyRuleResult, bool]:
    matched = not policy_input.moves_money
    return (
        PolicyRuleResult(rule_id="NO_MONEY_MOVEMENT_AUTO_ALLOW", matched=matched, outcome="ALLOW" if matched else None),
        matched,
    )


def _get_limit(config: dict[str, Any], key: str) -> int:
    if key not in config or config[key] is None:
        raise PolicyConfigError(f"merchant_policy_config missing required key '{key}'")
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PolicyConfigError(f"merchant_policy_config['{key}'] is invalid: {value!r}")
    return value


def check_auto_allow_under_limit(policy_input: PolicyInput) -> tuple[PolicyRuleResult, bool]:
    max_auto_capture_amount = _get_limit(policy_input.merchant_policy_config, "max_auto_capture_amount")
    matched = policy_input.amount <= max_auto_capture_amount
    return (
        PolicyRuleResult(rule_id="AUTO_ALLOW_UNDER_LIMIT", matched=matched, outcome="ALLOW" if matched else None),
        matched,
    )


def check_approval_band(policy_input: PolicyInput) -> tuple[PolicyRuleResult, bool]:
    max_auto_capture_amount = _get_limit(policy_input.merchant_policy_config, "max_auto_capture_amount")
    approval_band_upper = _get_limit(policy_input.merchant_policy_config, "approval_band_upper")
    if approval_band_upper < max_auto_capture_amount:
        raise PolicyConfigError(
            f"approval_band_upper ({approval_band_upper}) is less than "
            f"max_auto_capture_amount ({max_auto_capture_amount})"
        )
    matched = max_auto_capture_amount < policy_input.amount <= approval_band_upper
    return (
        PolicyRuleResult(rule_id="APPROVAL_BAND_CHECK", matched=matched, outcome="APPROVAL_REQUIRED" if matched else None),
        matched,
    )


def check_hard_limit(policy_input: PolicyInput) -> tuple[PolicyRuleResult, bool]:
    approval_band_upper = _get_limit(policy_input.merchant_policy_config, "approval_band_upper")
    matched = policy_input.amount > approval_band_upper
    return (
        PolicyRuleResult(rule_id="HARD_LIMIT_CHECK", matched=matched, outcome="BLOCK" if matched else None),
        matched,
    )
