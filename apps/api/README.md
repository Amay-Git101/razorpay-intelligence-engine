# apps/api

The full decision-intelligence pipeline plus the FastAPI HTTP surface that serves both the
API and the static frontend (`apps/web/`). See the [repository root README](../../README.md)
for the product story, architecture diagram, and demo instructions -- this file covers only
this package's local layout and setup.

## Layout

```text
src/domain/contracts.py   Pydantic contracts (ContextSnapshot, Decision, PolicyEvaluation,
                           Action, etc.) + provenance validation
src/db/                   DATABASE_URL -> psycopg connection, plain-SQL migrations
src/repository/           One module per table -- thin data-access functions only,
                           no business logic
src/razorpay_client/      Read-only Razorpay client -- no write method exists here
src/reconciliation/       Polls Razorpay, diffs state, records canonical events
src/intelligence/         RuleBasedEngine -- the deterministic decision module
src/policy/               Merchant policy_config enforcement
src/action/                The only module allowed to call Razorpay's capture endpoint
src/verification/         Independently re-confirms the outcome against Razorpay
src/feedback/             Verified-outcome -> expectation-baseline calibration
src/observability/        Read-only metrics aggregation
src/evaluation/           Independent RuleBasedEngine evaluation harness
src/pipeline/             Shared reconcile -> decide -> propose -> verify orchestration
src/manual_run/           CLI entry point over the shared pipeline
src/api/                  FastAPI HTTP surface (this app's backend + static frontend mount)
tests/                    pytest suite, including test_architecture_boundaries.py
test-checkout.html        Manual Razorpay Test Mode checkout page used for live verification
```

## Local setup

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Windows Git Bash
# .venv\Scripts\pip.exe install -e ".[dev]"  # PowerShell
```

```bash
.venv/Scripts/pytest tests/ -v
```

DB-dependent tests skip cleanly (not a failure) if `DATABASE_URL` is unset -- everything else
runs regardless.

## Running against a live database

```bash
export DATABASE_URL=postgresql://user:password@host:5432/dbname
export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx

.venv/Scripts/python -m db.run_migrations
.venv/Scripts/pytest tests/ -v
.venv/Scripts/python -m uvicorn api.app:app --reload
```

Open **http://127.0.0.1:8000/**.
