# apps/api

Phase 3 foundation only: PostgreSQL schema, domain contracts, and a
repository layer. No FastAPI app, no ingestion/context/intelligence/
policy/action/verification modules yet — those are later gates in the
same phase, pending review of this foundation.

## Layout

```text
src/domain/contracts.py   Pydantic contracts (ContextSnapshot, Decision,
                           PolicyEvaluation, Action, etc.) + provenance
                           validation + idempotency-key computation
src/db/migrations/        Plain SQL migrations (0001_init.sql = full schema)
src/db/connection.py      DATABASE_URL -> psycopg connection
src/db/run_migrations.py  Minimal migration runner (no Alembic)
src/repository/           One module per table — thin data-access functions only,
                           no business logic
tests/test_domain_contracts.py   Pure Python, no DB required
tests/test_db_invariants.py      Requires a live Postgres — see below
```

## Local setup

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Windows Git Bash
# .venv\Scripts\pip.exe install -e ".[dev]"  # PowerShell
```

Run the DB-independent tests (always available):

```bash
.venv/Scripts/pytest tests/test_domain_contracts.py -v
```

## Running the DB-dependent tests

**As of Phase 3 gate A–D, this project has no reachable PostgreSQL
instance** — Docker Desktop is not installed, and no native Postgres
install exists on the development machine. `tests/test_db_invariants.py`
is fully written but has not been executed. See the Phase 3 gate report
(conversation record / `docs/phase3-gate-a-d-report.md`) for unblock
options.

Once a Postgres instance is reachable:

```bash
export DATABASE_URL=postgresql://user:password@host:5432/razorpay_decision_intelligence
.venv/Scripts/python -m db.run_migrations
.venv/Scripts/pytest tests/ -v
```
