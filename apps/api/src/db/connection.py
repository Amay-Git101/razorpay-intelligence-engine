"""Database connectivity.

No connection pool, no ORM session machinery -- a single thin helper
around psycopg for this foundation gate. Reads DATABASE_URL from the
environment; raises clearly if it's unset rather than silently falling
back to a default that might point somewhere unintended.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Local-development convenience: load apps/api/.env if it exists, so the dev
# server and CLI runners pick up DATABASE_URL without every shell having to
# export it first.
#
# override=False is deliberate and load-bearing: a variable already present in
# the real environment always wins. On a deployed host (Render, CI) the
# platform's own configuration must never be silently replaced by a stray file
# that happened to be on disk. .env is gitignored, so in practice this only
# ever fires locally.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


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
