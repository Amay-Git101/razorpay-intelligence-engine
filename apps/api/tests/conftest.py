from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

# Load apps/api/.env before any fixture reads DATABASE_URL.
#
# Without this, whether the DB-backed tests run depended on import order:
# they only saw DATABASE_URL if some earlier test had already imported
# db.connection (which loads the file as a side effect). Running a subset
# could therefore silently skip the very tests it was meant to exercise,
# and report green. Loading it here makes "is a database available?" a
# property of the environment rather than of which tests happened to run
# first. override=False keeps a real environment variable winning, exactly
# as db/connection.py does.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


@pytest.fixture
def db_conn():
    """Yields a real psycopg connection wrapped in a transaction that is
    always rolled back at the end of the test, so tests never leave data
    behind or depend on each other's state.

    Skips cleanly (not a failure) if DATABASE_URL is unset -- this is the
    currently-blocked path documented in the Phase 3 gate report; these
    tests are authored and ready to run the moment a Postgres instance is
    available.
    """
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set -- no Postgres instance available in this environment")

    conn = psycopg.connect(url)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def committed_merchant(db_conn):
    """A merchant on a connection whose writes are COMMITTED, plus explicit
    teardown that removes everything created under it.

    Needed because provisioning/test_orders.py commits after each order on
    purpose: a Razorpay order that has been created exists whether or not a
    local transaction later succeeds, so holding a cohort open risks real
    orders with no local record. That production semantics is right, and it
    means those rows escape the rollback db_conn relies on. Rather than
    weaken the production code to suit the fixture, the fixture cleans up
    after itself.

    Yields (connection, merchant_id). The connection is separate from
    db_conn's transaction so commits here cannot interfere with it.
    """
    from repository.merchants import insert_merchant

    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not set")

    conn = psycopg.connect(url)
    merchant_id = str(insert_merchant(conn, "Committed Test Merchant", {}, {}))
    conn.commit()
    try:
        yield conn, merchant_id
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    delete from payment_experiment_orders
                     where experiment_id in (select id from payment_experiments where merchant_id = %s)
                    """,
                    (merchant_id,),
                )
                cur.execute("delete from payment_experiments where merchant_id = %s", (merchant_id,))
                # No canonical_events cleanup: that table is append-only by
                # database trigger and rejects DELETE outright. Tests using
                # this fixture must not create events -- provisioning only
                # writes orders and cohort rows.
                cur.execute(
                    "delete from payment_attempts where order_id in (select id from orders where merchant_id = %s)",
                    (merchant_id,),
                )
                cur.execute("delete from orders where merchant_id = %s", (merchant_id,))
                cur.execute("delete from merchants where id = %s", (merchant_id,))
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def demo_merchant_id(db_conn) -> str:
    from repository.merchants import insert_merchant

    return str(insert_merchant(db_conn, "Demo Merchant", {}, {}))
