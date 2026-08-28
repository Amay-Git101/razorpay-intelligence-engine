"""Database connectivity.

No connection pool, no ORM session machinery -- a single thin helper
around psycopg for this foundation gate. Reads DATABASE_URL from the
environment; raises clearly if it's unset rather than silently falling
back to a default that might point somewhere unintended.
"""

from __future__ import annotations

import os

import psycopg


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Example:\n"
            "  postgresql://user:password@localhost:5432/razorpay_decision_intelligence\n"
            "See apps/api/README.md for local setup options."
        )
    return url


def get_connection() -> psycopg.Connection:
    return psycopg.connect(get_database_url())
