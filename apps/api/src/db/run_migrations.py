"""Minimal migration runner.

Applies every .sql file in db/migrations in filename order, tracking what
has already run in a schema_migrations table. No Alembic -- for this
foundation gate a plain, transparent runner is enough and keeps the
dependency list minimal, per the explicit instruction not to add
infrastructure that isn't required for the vertical slice.

Usage:
    python -m db.run_migrations
"""

from __future__ import annotations

from pathlib import Path

from db.connection import get_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists schema_migrations (
                    filename    text primary key,
                    applied_at  timestamptz not null default now()
                )
                """
            )
            cur.execute("select filename from schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"skip  {path.name} (already applied)")
                continue
            print(f"apply {path.name}")
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "insert into schema_migrations (filename) values (%s)", (path.name,)
                )
            conn.commit()


if __name__ == "__main__":
    run_migrations()
    print("done")
