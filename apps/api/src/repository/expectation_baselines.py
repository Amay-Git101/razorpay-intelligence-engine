from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


def upsert_baseline(
    conn: psycopg.Connection,
    merchant_id: str,
    bucket_key: str,
    recovery_rate: float,
    sample_size: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into expectation_baselines (merchant_id, bucket_key, recovery_rate, sample_size)
            values (%s, %s, %s, %s)
            on conflict (merchant_id, bucket_key) do update set
                recovery_rate = excluded.recovery_rate,
                sample_size = excluded.sample_size,
                updated_at = now()
            """,
            (merchant_id, bucket_key, recovery_rate, sample_size),
        )


def get_baseline(conn: psycopg.Connection, merchant_id: str, bucket_key: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from expectation_baselines where merchant_id = %s and bucket_key = %s",
            (merchant_id, bucket_key),
        )
        return cur.fetchone()
