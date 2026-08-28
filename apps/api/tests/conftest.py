from __future__ import annotations

import os
import uuid

import psycopg
import pytest


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
def demo_merchant_id(db_conn) -> str:
    from repository.merchants import insert_merchant

    return str(insert_merchant(db_conn, "Demo Merchant", {}, {}))
