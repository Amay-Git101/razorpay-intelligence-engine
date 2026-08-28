"""Expectation + calibration-storage DB-integration tests. Requires live
Postgres. No IntelligenceEngine/RuleBasedEngine/Decision involved -- this
gate is Expectation only.
"""

from __future__ import annotations

import pytest

from intelligence.calibration import upsert_calibrated_baseline
from intelligence.expectation import CALIBRATED_SOURCE, ZERO_EVIDENCE_SOURCE, compute_expectation
from repository.merchants import insert_merchant


def test_zero_evidence_default_when_no_baseline_exists(db_conn, demo_merchant_id):
    expectation = compute_expectation(db_conn, demo_merchant_id, "error_reason:payment_failed")

    assert expectation.sample_size == 0
    assert expectation.source == ZERO_EVIDENCE_SOURCE
    assert expectation.expected_recovery_rate == 0.5
    # explicit label check -- this must never look like calibrated evidence
    assert expectation.source != CALIBRATED_SOURCE


def test_calibrated_value_returned_once_baseline_exists(db_conn, demo_merchant_id):
    upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_failed", 0.73, 25)

    expectation = compute_expectation(db_conn, demo_merchant_id, "error_reason:payment_failed")

    assert expectation.source == CALIBRATED_SOURCE
    assert expectation.sample_size == 25
    assert expectation.expected_recovery_rate == pytest.approx(0.73)


def test_compute_expectation_is_deterministic(db_conn, demo_merchant_id):
    upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_cancelled", 0.4, 10)

    first = compute_expectation(db_conn, demo_merchant_id, "error_reason:payment_cancelled")
    second = compute_expectation(db_conn, demo_merchant_id, "error_reason:payment_cancelled")

    assert first == second


def test_expectation_is_isolated_per_merchant(db_conn, demo_merchant_id):
    other_merchant_id = str(insert_merchant(db_conn, "Other Merchant", {}, {}))

    upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_failed", 0.8, 40)
    # other_merchant_id has no baseline for this bucket at all

    mine = compute_expectation(db_conn, demo_merchant_id, "error_reason:payment_failed")
    theirs = compute_expectation(db_conn, other_merchant_id, "error_reason:payment_failed")

    assert mine.source == CALIBRATED_SOURCE
    assert mine.expected_recovery_rate == pytest.approx(0.8)
    assert theirs.source == ZERO_EVIDENCE_SOURCE  # no leakage from demo_merchant_id's baseline
    assert theirs.sample_size == 0


def test_upsert_calibrated_baseline_rejects_out_of_range_recovery_rate(db_conn, demo_merchant_id):
    with pytest.raises(ValueError):
        upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_failed", 1.5, 10)


def test_upsert_calibrated_baseline_rejects_negative_sample_size(db_conn, demo_merchant_id):
    with pytest.raises(ValueError):
        upsert_calibrated_baseline(db_conn, demo_merchant_id, "error_reason:payment_failed", 0.5, -1)
