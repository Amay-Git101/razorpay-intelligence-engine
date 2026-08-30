"""Independent evaluation harness for the RuleBasedEngine intelligence
decision layer.

Remediated after an independent-ground-truth audit found that 19 of 27
cases in the original dataset cited RuleBasedEngine's own docstrings,
module comments, or branch ordering as their justification -- which is
circular: it answers "does the implementation behave the way its own
comments say it behaves" rather than "does the implementation agree
with an independently authored expectation." This module and its
dataset (datasets/evaluation/rule_based_engine_cases.json) were rebuilt
so every claim traces to one of exactly three sources, never to
rule_based.py itself:

  - project_defined: a specific section of razorpay_master_claude_code_
    handoff_v1.md or the architecture contract.
  - engineering_authored: the actual text of a prior gate-approval
    decision. Following a second, stricter audit pass, these citations
    were upgraded from "this session's retained memory of the approval
    message" to the actual git commit message for the commit that
    implemented that gate (verifiable independently via `git show
    <hash>` -- permanent, timestamped project history, not this
    session's recollection). A claim only earns this tier if a commit
    message explicitly documents it as a reviewed/locked decision, not
    merely as a description of what the resulting code does. Caveat
    honestly disclosed in the dataset's own report and README: the
    commit messages were themselves written by the assistant, not
    quoted verbatim from the user's original chat approval -- a step
    more independent than the code's own docstring, but not identical
    to a direct transcript.
  - inference_assumption: an explicitly unsettled judgment call, honestly
    flagged as such, never disguised as a stronger claim. Several claims
    that were previously mistiered as engineering_authored on nothing
    more than this evaluation's own real-time reasoning (no actual prior
    review event) were downgraded here to this tier during the same
    audit pass.

`observed_production` is not a valid tier and never will be: Policy can
allow or block the same Decision (see Scenario A/B), so a final Action/
Verification/Policy outcome is never proof that the underlying
recommendation was correct.

WHAT THIS CAN HONESTLY PROVE:
  - Whether RuleBasedEngine's decision_type agrees or disagrees, case by
    case, with a specific, version-controlled, independently sourced
    dataset -- decision_type is the sole hard-fail primary signal.
  - Two much weaker, explicitly secondary, per-case-optional signals
    (reason_category, confidence) exist ONLY where independently
    grounded; a case omits either field entirely rather than asserting
    a claim its cited source doesn't actually support.
  - Which conceptual behavior areas of the engine have at least one
    independently grounded (project_defined or engineering_authored)
    case, versus areas covered only by inference_assumption cases with
    no independent grounding at all -- see EvaluationReport's
    `behavior_areas_inference_only`.

WHAT THIS CANNOT PROVE:
  - Real-world production accuracy. The dataset is small and
    hand-curated, not sampled from production traffic.
  - That the dataset's own labels are objectively correct -- a
    project_defined label is only as good as the cited document
    section; an engineering_authored label reflects a decision made at
    a specific point in time; an inference_assumption label is
    explicitly unsettled, not fact.
  - Anything about Policy, Action, Verification, or business outcome.
  - Calibration quality -- no calibration metric is computed anywhere
    in this module.

Requires no DATABASE_URL, no PostgreSQL, no Razorpay credentials, and
no network access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

from domain.contracts import ContextSnapshot, DecisionType, Expectation
from intelligence.rule_based import RuleBasedEngine

_REPO_ROOT = Path(__file__).resolve().parents[4]
DATASET_PATH = _REPO_ROOT / "datasets" / "evaluation" / "rule_based_engine_cases.json"

TIERS = frozenset({"project_defined", "engineering_authored", "inference_assumption"})

REASON_CATEGORIES = frozenset({
    "customer_initiated_no_action",
})

CONFIDENCE_RULES = frozenset({"fixed_1_0", "equals_expected_recovery_rate"})


class DatasetValidationError(Exception):
    """Raised when the dataset file is structurally invalid."""


def _require_tier_and_reference(tier: str, reference: str, where: str) -> None:
    if tier not in TIERS:
        raise DatasetValidationError(f"{where}: tier {tier!r} is not one of {sorted(TIERS)}")
    if not reference or not reference.strip():
        raise DatasetValidationError(f"{where}: tier={tier} requires a non-empty reference")


class DecisionTypeExpectation(BaseModel):
    value: DecisionType
    tier: str
    reference: str

    @model_validator(mode="after")
    def _validate(self) -> "DecisionTypeExpectation":
        _require_tier_and_reference(self.tier, self.reference, "expected.decision_type")
        return self


class ReasonCategoryExpectation(BaseModel):
    value: str
    tier: str
    reference: str

    @model_validator(mode="after")
    def _validate(self) -> "ReasonCategoryExpectation":
        _require_tier_and_reference(self.tier, self.reference, "expected.reason_category")
        if self.value not in REASON_CATEGORIES:
            raise DatasetValidationError(
                f"expected.reason_category: value {self.value!r} is not one of {sorted(REASON_CATEGORIES)}"
            )
        return self


class ConfidenceExpectation(BaseModel):
    value: str
    tier: str
    reference: str

    @model_validator(mode="after")
    def _validate(self) -> "ConfidenceExpectation":
        _require_tier_and_reference(self.tier, self.reference, "expected.confidence")
        if self.value not in CONFIDENCE_RULES:
            raise DatasetValidationError(f"expected.confidence: value {self.value!r} is not one of {sorted(CONFIDENCE_RULES)}")
        return self


class ExpectedBlock(BaseModel):
    """Per-field provenance. decision_type is mandatory (the sole
    hard-fail primary signal); reason_category and confidence are each
    optional and independently sourced -- a project_defined decision_type
    never implies its confidence is also project_defined."""

    decision_type: DecisionTypeExpectation
    reason_category: ReasonCategoryExpectation | None = None
    confidence: ConfidenceExpectation | None = None


class EvaluationCase(BaseModel):
    case_id: str
    description: str
    behavior_area: str  # reporting/coverage tag only -- never compared against actual output, so it carries no provenance requirement
    context: ContextSnapshot
    expectation: Expectation
    expected: ExpectedBlock


class CaseResult(BaseModel):
    case_id: str
    behavior_area: str
    decision_type_tier: str
    expected_decision_type: str
    actual_decision_type: str
    decision_type_match: bool

    reason_category_evaluated: bool
    reason_category_match: bool | None = None

    confidence_evaluated: bool
    confidence_match: bool | None = None
    actual_confidence: float


class EvaluationReport(BaseModel):
    """See harness.py's module docstring for what this report does and
    does not prove."""

    total_cases: int
    matched_cases: int
    mismatched_cases: int
    mismatched_case_ids: list[str]
    confusion_matrix: dict[str, dict[str, int]]
    per_decision_type_matched: dict[str, str]
    tier_breakdown: dict[str, int]
    behavior_areas_covered: list[str]
    behavior_areas_with_independent_ground_truth: list[str]
    behavior_areas_inference_only: list[str]
    reason_category_agreement: str
    confidence_agreement: str
    case_results: list[CaseResult]
    caveat: str = (
        "This report reflects a small, hand-curated, synthetic evaluation dataset. "
        "It is NOT representative of production traffic and does NOT establish "
        "real-world production accuracy, precision, recall, or calibration quality. "
        "It shows only whether RuleBasedEngine's decision_type (the sole primary "
        "signal) agrees with a set of independently sourced expectations; "
        "reason_category and confidence are weaker, per-case-optional secondary "
        "signals, evaluated only where independently grounded."
    )


def load_dataset(path: Path = DATASET_PATH) -> list[EvaluationCase]:
    """Loads and structurally validates the dataset. Raises
    DatasetValidationError on any malformed case, an unrecognized tier
    (including a would-be "observed_production" value, which is not in
    TIERS and is therefore rejected here), or a duplicate case_id."""
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    cases = [EvaluationCase.model_validate(item) for item in raw]

    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise DatasetValidationError(f"duplicate case_id in dataset: {case.case_id}")
        seen.add(case.case_id)

    return cases


def _evaluate_case(engine: RuleBasedEngine, case: EvaluationCase) -> CaseResult:
    actual = engine.evaluate(case.context, case.expectation)

    decision_type_match = actual.decision_type == case.expected.decision_type.value

    reason_category_evaluated = case.expected.reason_category is not None
    reason_category_match = None
    if case.expected.reason_category is not None:
        actual_is_no_action = actual.decision_type == DecisionType.NO_ACTION
        actual_is_customer_cancelled = "CUSTOMER_CANCELLED" in actual.reason_codes
        reason_category_match = (
            case.expected.reason_category.value == "customer_initiated_no_action"
            and actual_is_no_action
            and actual_is_customer_cancelled
        )

    confidence_evaluated = case.expected.confidence is not None
    confidence_match = None
    if case.expected.confidence is not None:
        rule = case.expected.confidence.value
        if rule == "fixed_1_0":
            confidence_match = actual.confidence == 1.0
        elif rule == "equals_expected_recovery_rate":
            confidence_match = actual.confidence == case.expectation.expected_recovery_rate

    return CaseResult(
        case_id=case.case_id,
        behavior_area=case.behavior_area,
        decision_type_tier=case.expected.decision_type.tier,
        expected_decision_type=case.expected.decision_type.value.value,
        actual_decision_type=actual.decision_type.value,
        decision_type_match=decision_type_match,
        reason_category_evaluated=reason_category_evaluated,
        reason_category_match=reason_category_match,
        confidence_evaluated=confidence_evaluated,
        confidence_match=confidence_match,
        actual_confidence=actual.confidence,
    )


def evaluate_dataset(cases: list[EvaluationCase]) -> EvaluationReport:
    """Runs RuleBasedEngine.evaluate() once per case and compares actual
    output against each case's independently sourced expectation.
    Deterministic: identical input always produces an identical report.
    ONLY decision_type disagreement is a hard failure; reason_category
    and confidence disagreements are reported separately and never
    silently folded into `matches`."""
    engine = RuleBasedEngine()
    results = [_evaluate_case(engine, case) for case in cases]

    decision_types = sorted(dt.value for dt in DecisionType)
    confusion_matrix: dict[str, dict[str, int]] = {e: {a: 0 for a in decision_types} for e in decision_types}
    for result in results:
        confusion_matrix[result.expected_decision_type][result.actual_decision_type] += 1

    per_decision_type_matched: dict[str, str] = {}
    for decision_type in decision_types:
        relevant = [r for r in results if r.expected_decision_type == decision_type]
        matched = sum(1 for r in relevant if r.decision_type_match)
        per_decision_type_matched[decision_type] = f"{matched}/{len(relevant)}"

    tier_breakdown: dict[str, int] = {tier: 0 for tier in sorted(TIERS)}
    for result in results:
        tier_breakdown[result.decision_type_tier] += 1

    areas_to_tiers: dict[str, set[str]] = {}
    for result in results:
        areas_to_tiers.setdefault(result.behavior_area, set()).add(result.decision_type_tier)

    behavior_areas_covered = sorted(areas_to_tiers)
    behavior_areas_with_independent_ground_truth = sorted(
        area for area, tiers in areas_to_tiers.items() if tiers & {"project_defined", "engineering_authored"}
    )
    behavior_areas_inference_only = sorted(
        area for area, tiers in areas_to_tiers.items() if tiers == {"inference_assumption"}
    )

    matched_cases = sum(1 for r in results if r.decision_type_match)
    mismatched_case_ids = sorted(r.case_id for r in results if not r.decision_type_match)

    reason_evaluated = [r for r in results if r.reason_category_evaluated]
    reason_matched = sum(1 for r in reason_evaluated if r.reason_category_match)

    confidence_evaluated = [r for r in results if r.confidence_evaluated]
    confidence_matched = sum(1 for r in confidence_evaluated if r.confidence_match)

    return EvaluationReport(
        total_cases=len(cases),
        matched_cases=matched_cases,
        mismatched_cases=len(mismatched_case_ids),
        mismatched_case_ids=mismatched_case_ids,
        confusion_matrix=confusion_matrix,
        per_decision_type_matched=per_decision_type_matched,
        tier_breakdown=tier_breakdown,
        behavior_areas_covered=behavior_areas_covered,
        behavior_areas_with_independent_ground_truth=behavior_areas_with_independent_ground_truth,
        behavior_areas_inference_only=behavior_areas_inference_only,
        reason_category_agreement=f"{reason_matched}/{len(reason_evaluated)}",
        confidence_agreement=f"{confidence_matched}/{len(confidence_evaluated)}",
        case_results=results,
    )
