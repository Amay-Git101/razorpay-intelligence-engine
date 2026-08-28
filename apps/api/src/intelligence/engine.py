from __future__ import annotations

from typing import Protocol

from domain.contracts import ContextSnapshot, DecisionOutput, Expectation


class IntelligenceEngine(Protocol):
    """Every implementation (RuleBasedEngine now, a future
    MLDecisionEngine later) must produce the same DecisionOutput shape.
    Downstream code (persistence, and later Policy/Action/Audit) never
    branches on which implementation produced it."""

    def evaluate(self, context: ContextSnapshot, expectation: Expectation) -> DecisionOutput: ...
