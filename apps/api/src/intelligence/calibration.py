"""Baseline storage mechanics ONLY.

This module intentionally does NOT implement a real Outcome ->
Calibration feedback loop. Outcome (and the Verification module that
would produce one) don't exist yet -- those are later gates. Calling
upsert_calibrated_baseline() today is a manual/test-only operation; no
running workflow triggers it automatically. Wiring an actual feedback
loop is out of scope for this gate and should not be assumed to exist.
"""

from __future__ import annotations

import psycopg

from repository.expectation_baselines import upsert_baseline


def upsert_calibrated_baseline(
    conn: psycopg.Connection,
    merchant_id: str,
    bucket_key: str,
    recovery_rate: float,
    sample_size: int,
) -> None:
    if not (0.0 <= recovery_rate <= 1.0):
        raise ValueError(f"recovery_rate out of range [0,1]: {recovery_rate}")
    if sample_size < 0:
        raise ValueError(f"sample_size must be >= 0: {sample_size}")
    upsert_baseline(conn, merchant_id, bucket_key, recovery_rate, sample_size)
