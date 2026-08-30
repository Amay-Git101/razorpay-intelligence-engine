"""Evaluation harness tests. Pure Python -- no DATABASE_URL, no network,
no live Postgres.

Remediated after an independent-ground-truth audit found 19/27 original
cases cited RuleBasedEngine's own docstrings/comments/branch order as
their justification. This file now also carries a dedicated
anti-circularity test class that scans the real dataset's *raw text*
for the specific citation patterns the audit identified as circular, so
this problem cannot silently return.

Two different kinds of assertion live here, deliberately kept apart:

  - Harness-logic tests use small, INLINE, hand-built cases (not the
    real dataset) to prove the comparison/aggregation logic itself is
    correct, independent of what the real dataset currently says.
  - The final test in this file evaluates the REAL, committed dataset
    and asserts every case's decision_type currently agrees with
    RuleBasedEngine. If the real dataset ever disagrees, this test must
    fail loudly -- it must never be weakened, and no label may be
    silently rewritten to make it pass.
"""

from __future__ import annotations

import json
import re

from domain.contracts import ContextSnapshot, DecisionType, Expectation, ProvenanceBand, ProvenancedField
from evaluation.harness import (
    DATASET_PATH,
    TIERS,
    DatasetValidationError,
    EvaluationCase,
    load_dataset,
    evaluate_dataset,
)
from intelligence.rule_based import RuleBasedEngine


def _field(name: str, value, band: ProvenanceBand = ProvenanceBand.RAW, source: str = "razorpay_api_poll") -> ProvenancedField:
    return ProvenancedField(field=name, value=value, band=band, source=source)


def _inline_case(case_id: str, context: ContextSnapshot, expectation: Expectation, expected: dict, behavior_area: str = "inline_test") -> EvaluationCase:
    return EvaluationCase.model_validate({
        "case_id": case_id,
        "description": "inline harness-logic test case",
        "behavior_area": behavior_area,
        "context": context.model_dump(mode="json"),
        "expectation": expectation.model_dump(mode="json"),
        "expected": expected,
    })


_DEFAULT_EXPECTATION = Expectation(bucket_key="no_error_reason", expected_recovery_rate=0.5, sample_size=0, source="rule_v1_default")


# ---------------------------------------------------------------------------
# Harness-logic tests (inline cases, not the real dataset)
# ---------------------------------------------------------------------------

def test_known_agreeing_case_is_reported_as_matched():
    context = ContextSnapshot(order_id="o1", payment_attempt_id="p1", fields=[_field("status", "authorized"), _field("amount", 10000)])
    case = _inline_case(
        "agree_case", context, _DEFAULT_EXPECTATION,
        {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": "project_defined", "reference": "inline fixture reference"}},
    )

    report = evaluate_dataset([case])

    assert report.total_cases == 1
    assert report.matched_cases == 1
    assert report.mismatched_cases == 0
    assert report.case_results[0].decision_type_match is True


def test_known_disagreeing_case_causes_hard_failure_visibility():
    context = ContextSnapshot(order_id="o2", payment_attempt_id="p2", fields=[_field("status", "authorized"), _field("amount", 10000)])
    case = _inline_case(
        "disagree_case", context, _DEFAULT_EXPECTATION,
        {"decision_type": {"value": "NO_ACTION", "tier": "project_defined", "reference": "inline fixture reference"}},
    )

    report = evaluate_dataset([case])

    assert report.matched_cases == 0
    assert report.mismatched_cases == 1
    assert report.mismatched_case_ids == ["disagree_case"]
    assert report.case_results[0].actual_decision_type == "RECOMMEND_CAPTURE"


def test_reason_category_is_optional_and_evaluated_only_when_present():
    context = ContextSnapshot(
        order_id="o3", payment_attempt_id="p3",
        fields=[_field("status", "failed"), _field("error_source", "customer"), _field("error_reason", "payment_cancelled")],
    )
    case_without = _inline_case(
        "no_category_case", context, _DEFAULT_EXPECTATION,
        {"decision_type": {"value": "NO_ACTION", "tier": "engineering_authored", "reference": "inline fixture reference"}},
    )
    case_with_category = _inline_case(
        "with_category_case", context, _DEFAULT_EXPECTATION,
        {
            "decision_type": {"value": "NO_ACTION", "tier": "engineering_authored", "reference": "inline fixture reference"},
            "reason_category": {"value": "customer_initiated_no_action", "tier": "engineering_authored", "reference": "inline fixture reference"},
        },
    )

    report = evaluate_dataset([case_without, case_with_category])

    without_result = next(r for r in report.case_results if r.case_id == "no_category_case")
    with_result = next(r for r in report.case_results if r.case_id == "with_category_case")

    assert without_result.reason_category_evaluated is False
    assert without_result.reason_category_match is None
    assert with_result.reason_category_evaluated is True
    assert with_result.reason_category_match is True  # customer_source + CUSTOMER_CANCELLED reason code both present


def test_confidence_is_optional_and_evaluated_only_when_present():
    expectation = Expectation(bucket_key="error_reason:payment_failed", expected_recovery_rate=0.73, sample_size=25, source="rule_v1")
    context = ContextSnapshot(
        order_id="o4", payment_attempt_id="p4",
        fields=[_field("status", "failed"), _field("error_source", "gateway"), _field("error_reason", "payment_failed")],
    )
    with_confidence = _inline_case(
        "confidence_case", context, expectation,
        {
            "decision_type": {"value": "RECOMMEND_RETRY_PROMPT", "tier": "project_defined", "reference": "inline fixture reference"},
            "confidence": {"value": "equals_expected_recovery_rate", "tier": "engineering_authored", "reference": "inline fixture reference"},
        },
    )
    without_confidence = _inline_case(
        "no_confidence_case", context, expectation,
        {"decision_type": {"value": "RECOMMEND_RETRY_PROMPT", "tier": "project_defined", "reference": "inline fixture reference"}},
    )

    report = evaluate_dataset([with_confidence, without_confidence])

    with_result = next(r for r in report.case_results if r.case_id == "confidence_case")
    without_result = next(r for r in report.case_results if r.case_id == "no_confidence_case")

    assert with_result.confidence_evaluated is True
    assert with_result.confidence_match is True
    assert with_result.actual_confidence == 0.73
    assert without_result.confidence_evaluated is False
    assert without_result.confidence_match is None


def test_confusion_matrix_and_per_decision_type_counts():
    authorized = ContextSnapshot(order_id="o5", payment_attempt_id="p5", fields=[_field("status", "authorized")])
    order_level = ContextSnapshot(order_id="o6", payment_attempt_id=None, fields=[_field("status", "created")])
    cases = [
        _inline_case("c1", authorized, _DEFAULT_EXPECTATION, {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": "project_defined", "reference": "x"}}),
        _inline_case("c2", order_level, _DEFAULT_EXPECTATION, {"decision_type": {"value": "NO_ACTION", "tier": "engineering_authored", "reference": "x"}}),
    ]

    report = evaluate_dataset(cases)

    assert report.confusion_matrix["RECOMMEND_CAPTURE"]["RECOMMEND_CAPTURE"] == 1
    assert report.confusion_matrix["NO_ACTION"]["NO_ACTION"] == 1
    assert report.per_decision_type_matched["RECOMMEND_CAPTURE"] == "1/1"
    assert report.per_decision_type_matched["RECOMMEND_RETRY_PROMPT"] == "0/0"
    assert report.tier_breakdown == {"engineering_authored": 1, "inference_assumption": 0, "project_defined": 1}


def test_behavior_area_grounding_classification():
    grounded = ContextSnapshot(order_id="o7", payment_attempt_id="p7", fields=[_field("status", "authorized")])
    inference_only = ContextSnapshot(order_id="o8", payment_attempt_id="p8", fields=[_field("status", "refunded")])
    cases = [
        _inline_case("grounded_case", grounded, _DEFAULT_EXPECTATION, {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": "project_defined", "reference": "x"}}, behavior_area="area_a"),
        _inline_case("inference_case", inference_only, _DEFAULT_EXPECTATION, {"decision_type": {"value": "NO_ACTION", "tier": "inference_assumption", "reference": "x"}}, behavior_area="area_b"),
    ]

    report = evaluate_dataset(cases)

    assert "area_a" in report.behavior_areas_with_independent_ground_truth
    assert "area_b" not in report.behavior_areas_with_independent_ground_truth
    assert "area_b" in report.behavior_areas_inference_only
    assert "area_a" not in report.behavior_areas_inference_only


def test_case_validation_rejects_unrecognized_tier():
    context = ContextSnapshot(order_id="o9", payment_attempt_id="p9", fields=[_field("status", "authorized")])
    try:
        _inline_case(
            "bad_tier", context, _DEFAULT_EXPECTATION,
            {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": "observed_production", "reference": "x"}},
        )
        assert False, "expected DatasetValidationError for observed_production tier"
    except DatasetValidationError:
        pass


def test_case_validation_requires_non_empty_reference_for_every_tier():
    context = ContextSnapshot(order_id="o10", payment_attempt_id="p10", fields=[_field("status", "authorized")])
    for tier in TIERS:
        try:
            _inline_case(
                f"missing_reference_{tier}", context, _DEFAULT_EXPECTATION,
                {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": tier, "reference": ""}},
            )
            assert False, f"expected DatasetValidationError for empty reference with tier={tier}"
        except DatasetValidationError:
            pass


def test_harness_genuinely_invokes_rule_based_engine():
    context = ContextSnapshot(order_id="o11", payment_attempt_id="p11", fields=[_field("status", "authorized"), _field("amount", 777)])
    expected_output = RuleBasedEngine().evaluate(context, _DEFAULT_EXPECTATION)
    case = _inline_case(
        "real_engine_case", context, _DEFAULT_EXPECTATION,
        {"decision_type": {"value": expected_output.decision_type.value, "tier": "project_defined", "reference": "x"}},
    )

    report = evaluate_dataset([case])

    assert report.case_results[0].decision_type_match is True


def test_repeated_run_is_deterministic():
    context = ContextSnapshot(
        order_id="o12", payment_attempt_id="p12",
        fields=[_field("status", "failed"), _field("error_source", "gateway"), _field("error_reason", "payment_failed")],
    )
    expectation = Expectation(bucket_key="error_reason:payment_failed", expected_recovery_rate=0.6, sample_size=10, source="rule_v1")
    case = _inline_case(
        "determinism_case", context, expectation,
        {"decision_type": {"value": "RECOMMEND_RETRY_PROMPT", "tier": "project_defined", "reference": "x"}},
    )

    first = evaluate_dataset([case]).model_dump_json()
    second = evaluate_dataset([case]).model_dump_json()

    assert first == second


def test_duplicate_case_ids_are_rejected(tmp_path):
    context = ContextSnapshot(order_id="o13", payment_attempt_id="p13", fields=[_field("status", "authorized")])
    case = _inline_case("dup", context, _DEFAULT_EXPECTATION, {"decision_type": {"value": "RECOMMEND_CAPTURE", "tier": "project_defined", "reference": "x"}})
    duplicate_file = tmp_path / "dup_cases.json"
    duplicate_file.write_text(json.dumps([case.model_dump(mode="json"), case.model_dump(mode="json")]), encoding="utf-8")

    try:
        load_dataset(duplicate_file)
        assert False, "expected DatasetValidationError for duplicate case_id"
    except DatasetValidationError:
        pass


def test_malformed_case_fails_loudly(tmp_path):
    malformed_file = tmp_path / "malformed_cases.json"
    malformed_file.write_text(json.dumps([{"case_id": "incomplete"}]), encoding="utf-8")

    raised = False
    try:
        load_dataset(malformed_file)
    except Exception:
        raised = True
    assert raised, "expected a validation error for a malformed case"


# ---------------------------------------------------------------------------
# Real dataset: structural validation
# ---------------------------------------------------------------------------

def test_real_dataset_loads_and_is_structurally_valid():
    cases = load_dataset()
    assert 15 <= len(cases) <= 25


def test_real_dataset_has_no_duplicate_case_ids():
    cases = load_dataset()
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))


def test_real_dataset_every_tier_is_from_the_approved_vocabulary():
    cases = load_dataset()
    for case in cases:
        assert case.expected.decision_type.tier in {"project_defined", "engineering_authored", "inference_assumption"}
        assert case.expected.decision_type.tier != "observed_production"


def test_real_dataset_every_project_defined_claim_has_a_document_reference():
    cases = load_dataset()
    for case in cases:
        for field_name, field in (
            ("decision_type", case.expected.decision_type),
            ("reason_category", case.expected.reason_category),
            ("confidence", case.expected.confidence),
        ):
            if field is None:
                continue
            if field.tier == "project_defined":
                assert "§" in field.reference or "razorpay_master_claude_code_handoff" in field.reference or "architecture_contract" in field.reference, (
                    f"{case.case_id}.{field_name}: project_defined reference does not cite a project document"
                )


def test_real_dataset_every_engineering_authored_claim_cites_a_prior_decision():
    cases = load_dataset()
    for case in cases:
        for field_name, field in (
            ("decision_type", case.expected.decision_type),
            ("reason_category", case.expected.reason_category),
            ("confidence", case.expected.confidence),
        ):
            if field is None:
                continue
            if field.tier == "engineering_authored":
                assert field.reference and len(field.reference) > 20, (
                    f"{case.case_id}.{field_name}: engineering_authored claim needs a substantive prior-decision citation"
                )


def test_real_dataset_every_engineering_authored_claim_cites_a_checkable_git_commit():
    # Following the second audit pass, engineering_authored claims must
    # trace to an actual, independently verifiable git commit -- not a
    # vague "session record"/"retained conversation record" appeal to
    # this assistant's own memory, which cannot be checked by anyone
    # else. A 7+ character hex short-hash is required in the reference.
    hash_pattern = re.compile(r"\b[0-9a-f]{7,40}\b")
    cases = load_dataset()
    for case in cases:
        for field_name, field in (
            ("decision_type", case.expected.decision_type),
            ("reason_category", case.expected.reason_category),
            ("confidence", case.expected.confidence),
        ):
            if field is None:
                continue
            if field.tier == "engineering_authored":
                assert "git commit" in field.reference.lower(), (
                    f"{case.case_id}.{field_name}: engineering_authored claim must cite an actual git commit, not a memory of one"
                )
                assert hash_pattern.search(field.reference), (
                    f"{case.case_id}.{field_name}: engineering_authored claim must include a checkable commit hash"
                )
                assert "session record" not in field.reference.lower() and "retained record" not in field.reference.lower(), (
                    f"{case.case_id}.{field_name}: engineering_authored claim must not fall back to appealing to this session's own memory"
                )


def test_real_dataset_every_inference_assumption_is_explicitly_marked():
    cases = load_dataset()
    for case in cases:
        for field_name, field in (
            ("decision_type", case.expected.decision_type),
            ("reason_category", case.expected.reason_category),
            ("confidence", case.expected.confidence),
        ):
            if field is None:
                continue
            if field.tier == "inference_assumption":
                assert field.reference, f"{case.case_id}.{field_name}: inference_assumption must still explain itself"


def test_real_dataset_confidence_has_its_own_provenance_separate_from_decision_type():
    # The exact defect the audit found: a project_defined decision_type
    # must never silently imply its confidence claim is also
    # project_defined -- confidence must carry its own tier.
    cases = load_dataset()
    for case in cases:
        if case.expected.confidence is None:
            continue
        assert case.expected.confidence.tier in TIERS
        # confidence provenance is a structurally separate object from
        # decision_type provenance -- this assertion documents that
        # invariant rather than merely restating pydantic's type system.
        assert case.expected.confidence is not case.expected.decision_type


# ---------------------------------------------------------------------------
# Anti-circularity: the dataset's raw text must not cite the
# implementation as ground-truth justification.
# ---------------------------------------------------------------------------

_FORBIDDEN_CIRCULAR_CITATIONS = (
    "module docstring",
    "module comment",
    "branch 1", "branch 2", "branch 3", "branch 4", "branch 5", "branch 6",
    "DEFAULT_MAX_RETRY_ATTEMPTS",
)


def test_real_dataset_never_cites_rule_based_engine_docstrings_or_branch_structure():
    raw_text = DATASET_PATH.read_text(encoding="utf-8")
    offenders = [phrase for phrase in _FORBIDDEN_CIRCULAR_CITATIONS if phrase.lower() in raw_text.lower()]
    assert offenders == [], f"dataset references implementation structure as justification: {offenders}"


def test_real_dataset_never_cites_test_rule_based_engine_as_ground_truth():
    raw_text = DATASET_PATH.read_text(encoding="utf-8")
    assert "test_rule_based_engine" not in raw_text


def test_real_dataset_contains_no_observed_production_labels():
    raw_text = DATASET_PATH.read_text(encoding="utf-8")
    assert "observed_production" not in raw_text


def test_real_dataset_does_not_contain_programmatically_generated_labels():
    # A structural proxy: every case's expected.decision_type.reference
    # is a non-trivial, hand-written sentence (not a bare enum value,
    # not empty, not a templated placeholder).
    cases = load_dataset()
    for case in cases:
        reference = case.expected.decision_type.reference
        assert len(reference) > 20, f"{case.case_id}: reference looks templated/generated, not hand-authored"
        assert reference != case.expected.decision_type.value.value


def test_real_dataset_covers_multiple_behavior_areas():
    cases = load_dataset()
    areas = {c.behavior_area for c in cases}
    assert len(areas) >= 5


def test_real_dataset_produces_deterministic_serialized_results():
    cases = load_dataset()
    first = evaluate_dataset(cases).model_dump_json()
    second = evaluate_dataset(cases).model_dump_json()
    assert first == second


# ---------------------------------------------------------------------------
# Real dataset: the actual evaluation result. If this fails, STOP and
# investigate the disagreement -- do NOT edit the label or the
# implementation without reporting first.
# ---------------------------------------------------------------------------

def test_real_dataset_all_decision_types_currently_agree_with_rule_based_engine():
    cases = load_dataset()
    report = evaluate_dataset(cases)

    assert report.mismatched_cases == 0, (
        f"{report.mismatched_cases} case(s) disagree with RuleBasedEngine on decision_type: "
        f"{report.mismatched_case_ids}. Per the evaluation gate's investigation rule, this must "
        f"be investigated and reported -- never silently fixed by editing the label or the "
        f"implementation."
    )
