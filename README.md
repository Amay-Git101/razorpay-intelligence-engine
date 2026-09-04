# Payment Recovery Lab

**Razorpay Buildathon — AI Revenue Recovery track**

An agent that triages a merchant's failed-payment backlog: it detects revenue at
risk, classifies why each payment failed, selects a bounded intervention, gates
every action against deterministic merchant policy, independently verifies
whatever it does, and reports what happened to every rupee.

---

## The problem

When a payment fails, Razorpay tells the merchant it failed. It does not tell
them **which failures are worth chasing, what to do about each one, or when to
stop.**

That costs money in both directions. Doing nothing leaves recoverable revenue on
the table. A blind retry cron re-charges cards that were reported stolen, hammers
issuers that already declined permanently, and burns issuer trust chasing money
that was never recoverable.

**This is deliberately not "automatic payment capture."** Capture is the last
step of the funnel and Razorpay already ships it as a setting. A system whose
only lever is capture is a wrapper around a checkbox. The open question is
upstream, on failures: *what should happen next, and should anything happen at
all?*

---

## What it actually did

Two runs over an identically-seeded 40-order backlog — **₹9,97,268.23 at risk**,
reproducible from a fixed seed. The only variable is whether the diagnosis layer
was available.

| Outcome | With diagnosis | No model (control) |
|---|---|---|
| Automated retry prompt | 19 · ₹7,35,923 · 73.8% | **0** |
| Stopped by a stopping rule | 15 · ₹1,96,241 · 19.7% | 2 · ₹16,968 · 1.7% |
| Escalated to a human | 6 · ₹65,104 · 6.5% | **38 · ₹9,80,300 · 98.3%** |

Read the control column carefully — it is the safety argument, measured rather
than asserted. **Removing the model automates nothing.** It hands almost
everything to a human. The two stops that survive are retry-budget cases, which
are evaluated *before* the diagnosis is read and therefore do not depend on it.

Reproduce both:

```bash
cd apps/api && python -m seed.synthetic_backlog <merchant_id> --orders 40
```

```bash
cd apps/api && python -m manual_run.run_recovery_batch <merchant_id> --source synthetic
```

---

## Where the AI is, and why it is safe there

The model classifies **why a payment failed** — a root cause, a failure class
(`TRANSIENT` / `TERMINAL` / `AMBIGUOUS`), a retry recommendation, a confidence.
It decides nothing about money.

That is a genuine judgement. Razorpay's `error_reason` is structured but coarse:
`payment_failed` covers a temporary bank outage, a stolen-card block, and a bare
`"Payment failed."` — three different interventions. The distinguishing
information lives only in the issuer's free-text description.

```bash
cd apps/api && python -m evaluation.diagnosis_harness
```

> **5 error_reason values map to more than one failure class, costing the best
> possible lookup table 6 unavoidable errors.**

The baseline is *fitted on the answers themselves*, so no reason-code lookup can
do better — those six errors are irreducible. **The model's own score in that
report is self-consistent by construction and is not evidence of capability;
disregard it.** Only the structural finding survives, and it is checkable by
reading the corpus.

### Three safety properties, each with a test

1. **The model is never told what a payment is worth.** `FailureSignals` is a
   strict allowlist with no amount, no currency, no customer identity, and
   `extra="forbid"` — so passing an amount is an error, not a silent drop. It
   cannot be steered toward aggression on a large payment because it does not
   know the payment is large.
2. **The model cannot override a stopping rule.** The retry-budget check runs
   *before* the diagnosis is read at all. Rule order is a security property here,
   not a style choice.
3. **A degraded model produces more human review, never more automation.** No
   diagnosis, low confidence, `AMBIGUOUS`, or a self-contradictory result all
   route to escalation. See the control column above.

Enforced architecturally, not by convention: `policy/`, `action/`,
`verification/` and `repository/` **cannot import the diagnosis layer at all**,
and only `diagnosis/` may import an LLM SDK. Both are checked by tests.

### Where inference runs

**This deployment does not call a language model at request time.** Claude Opus 5
classified each failure-evidence pattern offline into
`datasets/diagnosis/failure_corpus.json`; `PrecomputedDiagnoser` serves those,
keyed on a fingerprint of the evidence, and a cache miss escalates rather than
guessing. Persisted diagnoses carry `model_version = claude-opus-5/diagnosis_v1/offline`
so a replay is distinguishable from a live call in the database.

`AnthropicDiagnoser` implements the same protocol and calls the API live;
`--live-model` with an `ANTHROPIC_API_KEY` swaps it in and changes nothing else.

---

## Real vs synthetic

Enforced by a database CHECK constraint, not by documentation.
`recovery_batches.source` must be `'razorpay_test_mode'` or `'synthetic'` — there
is no way to create a batch that does not declare which. The label rides on the
ledger as `money_is_real`, is returned by the API, and renders as a banner the UI
has no code path to omit.

Synthetic payments are only ever `failed`, and no failed payment can reach the
Razorpay write path, so the synthetic batch is **provably free of external
calls**.

The one real transaction, verified end to end in Razorpay Test Mode:

| | |
|---|---|
| Order | `order_TXueleNMbhnp2s` |
| Payment | `pay_TXv6XNq04gxHpe` |
| Amount | ₹500 |
| Initial state | `authorized`, `captured=false` |
| Pipeline | `RECOMMEND_CAPTURE` → `ALLOW` → `CAPTURE_PAYMENT` → `VERIFIED_SUCCESS` |
| Confirmed by | an independent re-read of Razorpay: `captured=true` |
| Resolution | 3.9 seconds |

---

## Measuring recovery

Two quantities, never merged — merging them is how this number becomes a lie.

- **Disposition**: a partition of the batch's frozen `revenue_at_risk` across
  mutually exclusive outcomes. Every paisa lands in exactly one bucket and the
  buckets sum to the total — *checked*, not assumed. The API returns
  `disposition_is_complete`, and the UI refuses to draw a breakdown that does not
  add up.
- **Verified recovery**: the sum of `outcome.recovered_amount` over captures that
  reached `VERIFIED_SUCCESS`, read from the action's own outcome.

Detection excludes orders already `paid`, and counts an order with three failed
attempts **once** — both enforced in SQL, because `revenue_at_risk` is the
denominator of every percentage reported and inflating it would flatter
everything.

`STOPPED` is reported as money no longer being pursued. Never as money recovered
or saved.

---

## Four problems you can run yourself

The site is four experiments, not a dashboard. Open it and pick one.

**01 · An authorized payment needs a decision.** Pick ₹500 or ₹8,000, pay a
real Test Mode order in Razorpay's own Checkout, and watch the pipeline read
the payment, recommend, gate on merchant policy, act only if allowed, and
verify with Razorpay what actually happened. The amount is the experiment:
one side of the merchant's configured limit is captured automatically, the
other is not.

**02 · Is the payment gateway having trouble?** Counts what this merchant's
recent payments actually did. Observed counts, then arithmetic, then
interpretation — kept apart so the conclusion can be checked against its
evidence. It reports concentration; it never claims an outage it cannot see.

**03 · Is this one payment failing, or are many failing?** Creates six real
Test Mode orders and freezes them as a group before any is paid. You decide
which succeed and which fail, in Razorpay's dialog. The conclusion is
computed from those six and changes when your results change.

**04 · Does the customer's previous payment behaviour change the decision?**
Pay more than once as the same payer. The history is real — Razorpay's
payment object carries the email and this system already stored it. History
can send a payment to a human for review; it can never buy it more
automation.

Every state shown comes from a real Razorpay operation, a real database
record, or a real calculation. Creating an order moves no money; only a
human completing Checkout can produce a payment, which is why the outcomes
are worth analysing.

## Running it

```bash
cd apps/api && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
```

Create `apps/api/.env` (gitignored — never commit it):

```
DATABASE_URL=postgresql://...
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
```

```bash
cd apps/api && python -m db.run_migrations
```

```bash
cd apps/api && .venv/Scripts/python -m uvicorn api.app:app --port 8010 --app-dir src
```

Frontend at `http://localhost:8010/`. Tests:

```bash
cd apps/api && python -m pytest -q
```

**365 passing** against a live Postgres. Without `DATABASE_URL` the DB-backed
tests skip rather than fail.

---

## Layout

| Path | |
|---|---|
| `apps/api/src/risk/` | revenue-at-risk detection (deterministic SQL) |
| `apps/api/src/diagnosis/` | the AI layer — the only package that may touch a model |
| `apps/api/src/intelligence/recovery_engine.py` | intervention selection (deterministic) |
| `apps/api/src/policy/` | merchant policy gate |
| `apps/api/src/action/` | bounded execution + the Razorpay write boundary |
| `apps/api/src/verification/` | independent re-read of external state |
| `apps/api/src/observability/batch_ledger.py` | batch recovery ledger |
| `apps/api/src/pipeline/recovery.py` | the batch workflow |
| `apps/api/tests/test_architecture_boundaries.py` | the boundaries above, enforced |
| `docs/ARCHITECTURE.md` | full architecture, incl. §9 honest limitations |

---

## Limitations

Summarised; the full list is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §9.

- The classifier is **unvalidated on real traffic**. The corpus is small and
  single-author, and its accuracy figures are self-consistent by construction.
- **Inference is offline.** No model is called at request time in this
  deployment.
- **Only one real payment moved money.** Authorising a Test Mode payment needs a
  human in a browser, so scale is demonstrated synthetically and labelled as such
  everywhere.
- `CUSTOMER_RETRY_PROMPT` **has no external effect** — it stops at `AUTHORIZED`
  and is reported as `RETRY_PENDING`, never as a recovery. Wiring it up means
  Razorpay Payment Links, which was scoped out.
- **Escalation is a queue, not a notification.** It records an auditable outcome;
  it does not email anyone.
- **Webhooks are not implemented** — event ingestion is API-poll only.
