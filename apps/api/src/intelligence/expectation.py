"""Expectation computation only. No IntelligenceEngine / RuleBasedEngine /
DecisionOutput here -- that's the next gate.
"""

from __future__ import annotations

import psycopg

from domain.contracts import Expectation
from repository.expectation_baselines import get_baseline

# Explicit marker for the zero-evidence fallback. Any consumer checking
# `expectation.source == ZERO_EVIDENCE_SOURCE` (or, equivalently,
# `sample_size == 0`) knows this number is a placeholder, not measured
# merchant evidence, and must not present it to a merchant as a
# calibrated statistic.
ZERO_EVIDENCE_SOURCE = "rule_v1_default"
CALIBRATED_SOURCE = "rule_v1"


def compute_expectation(conn: psycopg.Connection, merchant_id: str, bucket_key: str) -> Expectation:
    """Returns the calibrated baseline for (merchant_id, bucket_key) if
    one has been recorded, otherwise an explicit zero-evidence prior
    (expected_recovery_rate=0.5, sample_size=0). The 0.5 is an arbitrary
    uninformative midpoint, not a claim about this merchant's actual
    recovery behavior -- sample_size=0 is the signal that no real
    evidence backs it."""
    baseline = get_baseline(conn, merchant_id, bucket_key)
    if baseline is None:
        return Expectation(
            bucket_key=bucket_key,
            expected_recovery_rate=0.5,
            sample_size=0,
            source=ZERO_EVIDENCE_SOURCE,
        )
    return Expectation(
        bucket_key=bucket_key,
        expected_recovery_rate=float(baseline["recovery_rate"]),
        sample_size=baseline["sample_size"],
        source=CALIBRATED_SOURCE,
    )
