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
    # sequence_number (not created_at) is the ordering key -- created_at
    # is transaction-scoped in Postgres (identical for every statement in
    # one transaction) and cannot discriminate insertion order on its
    # own. See 0004_audit_entries_ordering_sequence.sql for the full
    # explanation.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from audit_entries where decision_id = %s order by sequence_number asc",
            (decision_id,),
        )
        return cur.fetchall()


def list_audit_trail(conn: psycopg.Connection, event_id: str | None, decision_id: str | None) -> list[dict[str, Any]]:
    """The complete chronological checkpoint sequence for one flow,
    combining both audit_entries columns that scope to it:
    EVENT_INGESTED is recorded against event_id only (it predates any
    Decision); everything from DECISION_CREATED onward is recorded
    against decision_id. Same query tests/support.py's full_audit_trail
    test helper already uses -- promoted here as a real, production
    read function so the API doesn't need to depend on test code."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from audit_entries where event_id = %s or decision_id = %s order by sequence_number asc",
            (event_id, decision_id),
        )
        return cur.fetchall()
