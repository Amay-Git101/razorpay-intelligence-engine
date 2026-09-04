"""Data access for guided-experiment cohorts (migration 0006).

Thin data access only, in keeping with every other module in this
package: no Razorpay call, no decision logic, no interpretation of the
outcomes it reads back. Whether four failures out of six means anything
is answered in risk/failure_patterns.py, not here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


def insert_experiment(
    conn: psycopg.Connection,
    merchant_id: str,
    kind: str,
    label: str | None = None,
) -> UUID:
    """Creates an empty cohort. `source` is left to its column default,
    which the CHECK constraint pins to 'razorpay_test_mode' -- there is
    no parameter for it because there is no other legal value."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into payment_experiments (merchant_id, kind, label)
            values (%s, %s, %s)
            returning id
            """,
            (merchant_id, kind, label),
        )
        return cur.fetchone()[0]


def insert_experiment_order(
    conn: psycopg.Connection,
    experiment_id: UUID | str,
    order_id: str,
    position: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into payment_experiment_orders (experiment_id, order_id, position)
            values (%s, %s, %s)
            on conflict (experiment_id, order_id) do nothing
            """,
            (experiment_id, order_id, position),
        )


def get_experiment(conn: psycopg.Connection, experiment_id: UUID | str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from payment_experiments where id = %s", (experiment_id,))
        return cur.fetchone()


def list_experiments_for_merchant(
    conn: psycopg.Connection, merchant_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select e.*, count(o.id) as order_count
              from payment_experiments e
              left join payment_experiment_orders o on o.experiment_id = e.id
             where e.merchant_id = %s
             group by e.id
             order by e.created_at desc
             limit %s
            """,
            (merchant_id, limit),
        )
        return cur.fetchall()


# The cohort's orders together with whatever payment state has been observed
# for each so far.
#
# The LEFT JOIN is the honest part: an order nobody has paid yet has no
# payment_attempts row, and this returns it with nulls rather than omitting
# it. Omitting unpaid orders would silently shrink the denominator -- "4 of
# 6 failed" would quietly become "4 of 4 failed" as soon as two orders were
# left unpaid, which is exactly the inflation this schema froze the cohort
# to prevent.
#
# distinct on picks one payment attempt per order: the most recently observed
# one, which carries the current state of that order's payment.
_COHORT_SQL = """
select
    peo.position,
    peo.order_id,
    o.amount,
    o.currency,
    o.status                as order_status,
    latest.id               as payment_attempt_id,
    latest.status           as payment_status,
    latest.captured         as payment_captured,
    latest.method           as payment_method,
    latest.error_reason     as error_reason,
    latest.error_step       as error_step,
    latest.error_source     as error_source,
    latest.observed_at      as payment_observed_at
from payment_experiment_orders peo
join orders o on o.id = peo.order_id
left join lateral (
    select p.*
      from payment_attempts p
     where p.order_id = peo.order_id
     order by p.observed_at desc, p.id desc
     limit 1
) latest on true
where peo.experiment_id = %s
order by peo.position
"""


def list_experiment_orders_with_state(
    conn: psycopg.Connection, experiment_id: UUID | str
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COHORT_SQL, (experiment_id,))
        return cur.fetchall()
