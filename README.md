# Decision Intelligence Console — Razorpay Payment Recovery

**Payments don't need to end at `AUTHORIZED`.** An explainable payment decision and recovery
engine for Razorpay: it observes payment state, evaluates recovery opportunities, applies
merchant policy, executes authorized actions, and independently verifies the outcome — every
step recorded to an audit trail.

**[Live demo →](#public-deployment)** &nbsp;·&nbsp; **[5-minute pitch video →](#5-minute-pitch)** &nbsp;·&nbsp; Razorpay Buildathon submission

<a name="5-minute-pitch"></a>
> **5-minute pitch video:** _TODO — add the video URL here once recorded._

![Payments don't need to end at AUTHORIZED — Observe, Reason, Authorize, Act, Verify](docs/preview.svg)

---

## Problem

A payment that reaches Razorpay `authorized` but isn't captured — a manual-capture merchant
who forgets to act, a timing gap, a missed webhook — is revenue sitting on the table until
Razorpay's authorization window expires. Handling this safely means answering four questions
correctly, every time: *should we act on this payment, are we allowed to, did the action
actually happen, and can we prove all of it afterward?* Getting any one of those wrong with a
money-moving operation is a real cost, not a bug report.

## What we built

A backend pipeline plus a live console that turns a raw Razorpay payment-state change into an
audited, policy-bounded action:

```
OBSERVE  → Reconciliation polls Razorpay's Orders/Payments API and diffs it
           against what we already know.
REASON   → RuleBasedEngine evaluates the change and proposes a decision
           (e.g. RECOMMEND_CAPTURE), with explicit reason codes.
AUTHORIZE→ Policy checks the proposal against the merchant's own configured
           limits and returns ALLOW / APPROVAL_REQUIRED / BLOCK.
ACT      → Only an ALLOWed action reaches the one write path in the system —
           a bounded call to Razorpay's capture endpoint.
VERIFY   → The outcome is independently re-confirmed against a fresh
           Razorpay read, never assumed from the write call's response.
```

Every one of those five steps is a real, separately-testable module — not a demo shortcut.

## Live verified flow

This exact sequence was run against a real Razorpay **Test Mode** order and a live Postgres
database (not a fixture, not a recording):

```
₹500 · Razorpay Test Mode
AUTHORIZED, captured=false
        ↓
RECOMMEND_CAPTURE      (RuleBasedEngine: status == "authorized")
        ↓
ALLOW                  (Policy: amount within merchant's auto-capture limit)
        ↓
CAPTURE_PAYMENT        (Action: the one bounded write path — POST /payments/:id/capture)
        ↓
VERIFIED_SUCCESS       (Verification: re-fetched from Razorpay, status + amount matched)
        ↓
₹500 recovered
```

The live console's **Live proof** section reads this same order from the running API in
real time — see [`GET /orders/{order_id}/timeline`](#decision--policy--action--verification-explanation).

## Architecture

```mermaid
flowchart LR
    RZP[(Razorpay Test Mode)] -- poll --> REC[Reconciliation]
    REC -- canonical event --> DEC[RuleBasedEngine\nDecision]
    DEC -- proposal --> POL[Policy\nmerchant policy_config]
    POL -- ALLOW --> ACT[Action\nCAPTURE_PAYMENT]
    ACT -- write --> RZP
    ACT -- proposed action --> VER[Verification]
    VER -- independent read --> RZP
    VER -- terminal result --> AUD[(Audit trail\nappend-only)]
    DEC -. every step .-> AUD
    POL -. every step .-> AUD
    ACT -. every step .-> AUD

    subgraph Console [Decision Intelligence Console — apps/web]
        UI[Dashboard + Order detail]
    end
    API[FastAPI — apps/api/src/api] -- reads/serves --> UI
    AUD --> API
    UI -- POST reconcile --> API
    API -- calls --> REC
```

One Postgres database (Neon) is the single source of truth; the FastAPI service (`apps/api`)
is a thin, read-mostly HTTP surface over the same pipeline the CLI runner uses — the frontend
never talks to Postgres or Razorpay directly.

## Decision → Policy → Action → Verification explanation

| Stage | Module | What it does | What it never does |
|---|---|---|---|
| Reconciliation | `src/reconciliation/` | Fetches Razorpay order/payment state, diffs it, records canonical events | Guess at state it hasn't fetched |
| Decision | `src/intelligence/rule_based.py` | Deterministic `RuleBasedEngine` — fixed rules, explicit reason codes | Score confidence from anything but the observed rule condition |
| Policy | `src/policy/` | Checks a proposal against the merchant's own `policy_config` (`max_auto_capture_amount`, `approval_band_upper`) | Allow a money-moving action past a missing/invalid config — it fails closed |
| Action | `src/action/` | The **only** module that may call `RazorpayWriteClient.capture_payment()` | Execute anything Policy didn't `ALLOW` |
| Verification | `src/verification/` | Independently re-fetches the payment from Razorpay before declaring success | Trust the write call's own response as the terminal outcome |

Every one of these boundaries — which module may import what, which module may write, which
module may call Razorpay — is enforced by `apps/api/tests/test_architecture_boundaries.py`,
not just by convention or code review.

## Why deterministic rules, not ML

**`RuleBasedEngine` — deterministic `rule_v1`.** Payment capture is a money-moving operation,
so v1 deliberately prioritizes:

- **Explainability** — every decision carries the exact reason codes that produced it.
- **Deterministic behavior** — the same observed state always yields the same decision.
- **Explicit policy enforcement** — merchant-configured limits gate every action, unconditionally.
- **Auditability** — every step is written to an append-only audit trail.
- **Independent verification** — no action is trusted without re-confirming it against Razorpay.

The system already records verified capture outcomes against each decision's expectation
baseline (`src/feedback/`) — the foundation for, but not yet, a calibrated model:

```
historical verified outcomes → calibrated decision intelligence → improved recovery recommendations
```

This is a stated direction, not a current capability. Nothing in this project calls
`RuleBasedEngine` an ML model or a trained model, anywhere — including in its own UI, and that
claim is enforced by a test (`test_frontend_never_calls_the_engine_an_ml_model`).

## Failure recovery story

**Symptom:** an initial Razorpay Test Mode transaction unexpectedly reached `captured` state
before our reconciliation flow ever observed it as `authorized` / `captured: false` — so the
recovery pipeline had nothing to act on.

**We investigated, in order:** the fetched payment state, how the order had been created, the
Checkout configuration used to pay it, the read-client adapter's invocation, reconciliation's
status-to-event mapping, and whether our own code had somehow triggered a capture.

**Finding:** our code never captured anything — the read-only Razorpay client
(`RazorpayReadClient`) exposes no write method at all, and the write path
(`RazorpayWriteClient.capture_payment`) is never invoked by reconciliation; an
architecture-boundary test proves this structurally. The order itself had reached Razorpay
already captured, most likely because it wasn't created with an explicit manual-capture
configuration, unlike an earlier, correctly-behaving reference order from our own Phase 1
verification (`docs/phase1-verification-report.md`).

**Fix:** we created a fresh Test Mode order with explicit manual capture. It correctly landed
in `authorized` / `captured: false`. Our system then produced
`RECOMMEND_CAPTURE → ALLOW → CAPTURE_PAYMENT → VERIFIED_SUCCESS`, and Razorpay independently
confirmed `captured: true` — the live verified flow shown above.

## Tech stack

- **Backend:** Python, FastAPI, `psycopg` (PostgreSQL driver), `httpx` (Razorpay HTTP calls), Pydantic
- **Database:** PostgreSQL (Neon, serverless)
- **Frontend:** vanilla HTML/CSS/JS — no build step, no framework, no Node dependency
- **Testing:** `pytest`, with a dedicated architecture-boundary test suite (pure Python, no DB)
- **Payments:** Razorpay Test Mode (Orders / Payments read API + capture write endpoint)

No React/Next/Node, no webhooks, no authentication layer, no ML framework — all deliberate
scope decisions for this stage, not oversights (see [Known limitations](#known-limitations--future-direction)).

## Repository structure

```text
apps/
  api/
    src/
      domain/          Pydantic contracts shared across every module
      db/               DATABASE_URL -> psycopg connection, migrations
      repository/       One module per table -- thin data access, no business logic
      razorpay_client/  Read-only Razorpay client (client.py) -- no write method exists here
      reconciliation/   Polls Razorpay, diffs state, records canonical events
      intelligence/     RuleBasedEngine -- the deterministic decision module
      policy/           Merchant policy_config enforcement (ALLOW/APPROVAL_REQUIRED/BLOCK)
      action/           The one module allowed to call Razorpay's capture endpoint
      verification/     Independently re-confirms the outcome against Razorpay
      feedback/         Verified-outcome -> expectation-baseline calibration infrastructure
      observability/    Read-only metrics aggregation over decisions/actions
      evaluation/       Independent RuleBasedEngine evaluation harness
      pipeline/         Shared reconcile -> decide -> propose -> verify orchestration
      manual_run/       CLI entry point over the shared pipeline
      api/              FastAPI HTTP surface over the shared pipeline (this app's backend)
    tests/               pytest suite, including test_architecture_boundaries.py
    test-checkout.html   Manual Razorpay Test Mode checkout page used for live verification
  web/                  Static frontend served by api/app.py's StaticFiles mount
    index.html, app.js, style.css
docs/                    Verification reports, ADRs, this README's preview graphic
render.yaml               Deployment blueprint (see "Public deployment" below)
```

## Local setup

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"          # Windows Git Bash
# .venv\Scripts\pip.exe install -e ".[dev]"    # PowerShell

export DATABASE_URL=postgresql://user:password@host:5432/dbname
export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx

.venv/Scripts/python -m db.run_migrations
.venv/Scripts/python -m uvicorn api.app:app --reload
```

Open **http://127.0.0.1:8000/**.

## Demo instructions

1. Open the app — you land on the product overview (this page's hero, how-it-works, live
   proof, AI-judgment, and failure-recovery sections).
2. Click **Explore the live console** (or the **Console** nav link) to see live merchant data:
   observability metrics and an orders table, read directly from the API.
3. Click any order to open its detail page — pipeline tracker, decision/policy/action/
   verification cards, and the append-only audit trail.
4. **Reconcile Order** re-polls Razorpay for that specific order and processes any new events
   through the full decision pipeline live, in front of you.

## Known limitations / future direction

- No webhook ingestion — reconciliation is on-demand (manual trigger or CLI sweep), by design
  for this stage; see the architecture contract for the planned webhook path.
- No merchant switcher in the console — the dashboard shows the most recently created
  merchant; adding a selector is a UI-only change, not an architecture change.
- `RuleBasedEngine` is deterministic, not learned — see "Why deterministic rules, not ML" above.
- No authentication layer — this is a Test Mode demo surface, not a production merchant panel.
- Calibrated decision intelligence from historical verified outcomes is a stated direction,
  not yet built (the underlying expectation-baseline data is already being recorded).

<a name="public-deployment"></a>
## Public deployment

The app is a single FastAPI service (`apps/api`) that also serves the static frontend — no
separate frontend deployment is needed. A [Render](https://render.com) Blueprint
(`render.yaml`) is included at the repo root; deployment itself has not been performed (it
requires your own Razorpay/Neon credentials, which this project never has access to).

**To deploy on Render:**

1. Push this repository to a public GitHub repo (or connect the private one).
2. In Render: **New → Blueprint**, select this repo — Render reads `render.yaml` automatically.
3. When prompted, set these environment variables (Render marks them `sync: false`, so you
   enter them yourself — they are never committed to the repo):
   - `DATABASE_URL` — your Neon Postgres connection string
   - `RAZORPAY_KEY_ID` — your Razorpay Test Mode key id
   - `RAZORPAY_KEY_SECRET` — your Razorpay Test Mode key secret
4. Deploy. Render builds with `pip install -e .` and starts with
   `uvicorn api.app:app --host 0.0.0.0 --port $PORT`, both from `apps/api`.
5. Once live, run migrations once against the same `DATABASE_URL` from your machine:
   `DATABASE_URL=... .venv/Scripts/python -m db.run_migrations`.
6. Update the **Live demo** link at the top of this README with the public URL Render gives you.

Any other Python-hosting provider (Railway, Fly.io, a plain VM) works the same way: install
`apps/api` with `pip install -e .`, run `uvicorn api.app:app --host 0.0.0.0 --port $PORT` from
that directory, and set the same three environment variables.
