from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

# Append-only: this module intentionally exposes no update/delete
# functions. The database also rejects UPDATE/DELETE on this table via
# trg_audit_entries_append_only -- this is defense in depth, not a
# substitute for it.


def insert_audit_entry(
    conn: psycopg.Connection,
    checkpoint: str,
    snapshot: dict[str, Any],
    event_id: str | None = None,
    decision_id: str | None = None,
    action_id: str | None = None,
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_entries (event_id, decision_id, action_id, checkpoint, snapshot)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (event_id, decision_id, action_id, checkpoint, psycopg.types.json.Json(snapshot)),
        )
        return cur.fetchone()[0]


def list_audit_trail_for_decision(conn: psycopg.Connection, decision_id: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from audit_entries where decision_id = %s order by created_at asc",
            (decision_id,),
        )
        return cur.fetchall()
