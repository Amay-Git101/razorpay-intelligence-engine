"""Batch progress: the risk package's own orchestration surface over
recovery_batch_items.

This module exists because of an architecture rule the test suite enforces
(test_pipeline_module_repository_imports_are_limited_to_event_resolution):
src/pipeline/ sequences stages, it does not own writes. Every other stage
already follows that shape -- pipeline calls make_decision(),
propose_action(), verify_action(), and each of those owns its own
persistence inside the package the data belongs to.

Batch items belong to risk/, which created them. So the batch loop calls
these two functions rather than reaching into repository.recovery_batches
itself. Same pattern, applied to the one new piece of state this gate added.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from repository.recovery_batches import link_item_decision, list_unprocessed_items


def claim_unprocessed_items(
    conn: psycopg.Connection, batch_id: UUID | str, limit: int | None = None
) -> list[dict[str, Any]]:
    """The batch's detected-but-not-yet-decided items, largest amount first.

    Ordering by amount is deliberate: if a run is cut short -- by a limit, a
    timeout, or an operator stopping it -- the money that got worked is the
    money that mattered most.
    """
    items = list_unprocessed_items(conn, batch_id)
    return items[:limit] if limit is not None else items


def record_item_decision(
    conn: psycopg.Connection, batch_id: UUID | str, payment_attempt_id: str, decision_id: UUID | str
) -> None:
    """Attach the decision that was reached for one at-risk payment.

    This is what moves an item out of NOT_YET_PROCESSED in the ledger, so it
    is written in the same transaction as the decision it names -- an item
    can never point at a decision that was rolled back.
    """
    link_item_decision(conn, batch_id, payment_attempt_id, decision_id)
