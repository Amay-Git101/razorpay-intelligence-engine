"""Regression tests for deterministic audit_entries ordering.

Root cause (full explanation in
src/db/migrations/0004_audit_entries_ordering_sequence.sql):
audit_entries.created_at defaults to Postgres's now(), which returns the
START TIME OF THE ENCLOSING TRANSACTION -- identical for every statement
executed within one transaction, not the wall-clock moment of each
individual INSERT. Every test in this project runs inside one long-lived
transaction (rolled back at teardown), so audit rows written during a
single test can share an identical created_at value, making
`order by created_at asc` produce ties with no defined tie-breaking
order. `sequence_number` (a bigint identity column) is the actual
ordering key repository.audit.list_audit_trail_for_decision and
support.full_audit_trail now use.
"""

from __future__ import annotations

from repository.audit import insert_audit_entry, list_audit_trail_for_decision
from support import FakeReconciliationClient, full_audit_trail, insert_capture_decision
from reconciliation.service import reconcile_order
from repository.canonical_events import list_events_for_order


def test_audit_entries_in_one_transaction_can_share_created_at(db_conn):
    """Empirically proves the root cause exists in this environment: rows
    inserted back-to-back within the same transaction get an IDENTICAL
    created_at, so created_at alone cannot order them."""
    id1 = insert_audit_entry(db_conn, "DECISION_CREATED", {"n": 1})
    id2 = insert_audit_entry(db_conn, "POLICY_EVALUATED", {"n": 2})
    id3 = insert_audit_entry(db_conn, "ACTION_AUTHORIZED", {"n": 3})

    with db_conn.cursor() as cur:
        cur.execute(
            "select distinct created_at from audit_entries where id in (%s, %s, %s)", (id1, id2, id3)
        )
        distinct_timestamps = cur.fetchall()
    assert len(distinct_timestamps) == 1, (
        "expected all three rows written in this transaction to share one "
        "transaction-scoped created_at value -- if this now fails, Postgres "
        "behavior around now()/transaction_timestamp() has changed and the "
        "reasoning behind the sequence_number fix should be re-examined"
    )


def test_sequence_number_matches_true_insertion_order_despite_created_at_tie(db_conn):
    id1 = insert_audit_entry(db_conn, "DECISION_CREATED", {"n": 1})
    id2 = insert_audit_entry(db_conn, "POLICY_EVALUATED", {"n": 2})
    id3 = insert_audit_entry(db_conn, "ACTION_AUTHORIZED", {"n": 3})

    with db_conn.cursor() as cur:
        cur.execute(
            "select id from audit_entries where id in (%s, %s, %s) order by sequence_number asc",
            (id1, id2, id3),
        )
        ordered_ids = [row[0] for row in cur.fetchall()]

    assert ordered_ids == [id1, id2, id3]


def _real_event_and_decision(db_conn, demo_merchant_id, order_id: str):
    """audit_entries.event_id/decision_id are real foreign keys -- this
    creates genuine canonical_events/decisions rows to attach test audit
    entries to, rather than fabricated UUIDs that would violate the FK
    constraint."""
    payment_id = f"pay_{order_id}"
    client = FakeReconciliationClient(order_id, payment_id, amount=10000)
    reconcile_order(db_conn, client, demo_merchant_id, order_id)
    event = next(e for e in list_events_for_order(db_conn, order_id) if e["event_type"] == "payment.attempt.authorized")
    decision_id = insert_capture_decision(db_conn, demo_merchant_id, order_id, payment_id, amount=10000)
    return str(event["id"]), str(decision_id)


def test_list_audit_trail_for_decision_returns_true_chronological_order(db_conn, demo_merchant_id):
    _event_id, decision_id = _real_event_and_decision(db_conn, demo_merchant_id, "order_audit_order_decision")

    insert_audit_entry(db_conn, "DECISION_CREATED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "POLICY_EVALUATED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "ACTION_AUTHORIZED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "ACTION_EXECUTED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "VERIFICATION_COMPLETED", {}, decision_id=decision_id)

    trail = list_audit_trail_for_decision(db_conn, decision_id)
    checkpoints = [row["checkpoint"] for row in trail]

    assert checkpoints == [
        "DECISION_CREATED", "POLICY_EVALUATED", "ACTION_AUTHORIZED", "ACTION_EXECUTED", "VERIFICATION_COMPLETED",
    ]


def test_full_audit_trail_combined_query_returns_true_chronological_order(db_conn, demo_merchant_id):
    # This is the exact query shape (event_id OR decision_id) that
    # originally surfaced the bug -- a single-column WHERE happened to
    # look correct by incidental physical ordering; this one didn't.
    #
    # _real_event_and_decision() calls reconcile_order(), which -- as a
    # correct, real side effect of exercising production reconciliation
    # code -- already writes a genuine EVENT_INGESTED entry for the
    # authorized event. No separate EVENT_INGESTED insert belongs here;
    # adding one duplicated it (this was the actual cause of the extra
    # row in the failing run, not a query defect).
    event_id, decision_id = _real_event_and_decision(db_conn, demo_merchant_id, "order_audit_full_trail")

    insert_audit_entry(db_conn, "DECISION_CREATED", {}, event_id=event_id, decision_id=decision_id)
    insert_audit_entry(db_conn, "POLICY_EVALUATED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "ACTION_AUTHORIZED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "ACTION_EXECUTED", {}, decision_id=decision_id)
    insert_audit_entry(db_conn, "VERIFICATION_COMPLETED", {}, decision_id=decision_id)

    checkpoints = full_audit_trail(db_conn, event_id, decision_id)

    assert checkpoints == [
        "EVENT_INGESTED", "DECISION_CREATED", "POLICY_EVALUATED",
        "ACTION_AUTHORIZED", "ACTION_EXECUTED", "VERIFICATION_COMPLETED",
    ]
