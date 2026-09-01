from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


def insert_merchant(
    conn: psycopg.Connection,
    name: str,
    policy_config: dict[str, Any],
    automation_limits: dict[str, Any],
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into merchants (name, policy_config, automation_limits)
            values (%s, %s, %s)
            returning id
            """,
            (name, psycopg.types.json.Json(policy_config), psycopg.types.json.Json(automation_limits)),
        )
        return cur.fetchone()[0]


def get_merchant(conn: psycopg.Connection, merchant_id: UUID) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from merchants where id = %s", (merchant_id,))
        return cur.fetchone()


def list_merchants(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from merchants order by created_at desc limit %s", (limit,))
        return cur.fetchall()
