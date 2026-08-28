from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

# Append-only: this module intentionally exposes no update/delete
# functions. The database also rejects UPDATE/DELETE on this table via
# trg_canonical_events_append_only -- this is defense in depth, not a
# substitute for it.


def insert_canonical_event(
    conn: psycopg.Connection,
    merchant_id: str,
    event_type: str,
    source: str,
    entity_type: str,
    entity_id: str,
    order_id: str,
    occurred_at: datetime,
    payload: dict[str, Any],
    source_reference: str | None = None,
    payload_version: str = "v1",
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into canonical_events
                (merchant_id, event_type, source, entity_type, entity_id,
                 order_id, occurred_at, payload, source_reference, payload_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                merchant_id, event_type, source, entity_type, entity_id,
                order_id, occurred_at, psycopg.types.json.Json(payload),
                source_reference, payload_version,
            ),
        )
        return cur.fetchone()[0]


def list_events_for_order(conn: psycopg.Connection, order_id: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from canonical_events where order_id = %s order by occurred_at asc",
            (order_id,),
        )
        return cur.fetchall()
