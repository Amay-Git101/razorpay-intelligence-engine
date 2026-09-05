<p align="center">
  <img src="docs/preview.svg" alt="Payment Recovery Lab — an agent that triages a merchant's failed-payment backlog" width="100%">
</p>

<p align="center">
  <a href="https://razorpay-decision-intelligence.onrender.com"><img alt="Live site" src="https://img.shields.io/badge/%E2%96%B6_live-razorpay--decision--intelligence.onrender.com-2563eb?style=for-the-badge"></a>
</p>

<p align="center">
  <img alt="Tests" src="https://img.shields.io/badge/tests-441_passing-15803d?style=flat-square">
  <img alt="Python" src="https://img.shields.io/badge/python-3.13-2563eb?style=flat-square">
  <img alt="Razorpay" src="https://img.shields.io/badge/Razorpay-Test_Mode-3395ff?style=flat-square">
  <img alt="Track" src="https://img.shields.io/badge/Buildathon-AI_Revenue_Recovery-020817?style=flat-square">
</p>

# Payment Recovery Lab

**When a payment fails, Razorpay tells the merchant it failed. It does not tell
them which failures are worth chasing, what to do about each one, or when to
stop.**

This is an agent that answers those three questions — and, more to the point, one
that is *architecturally incapable* of answering them recklessly.

> ### → **[Go press the buttons](https://razorpay-decision-intelligence.onrender.com)**
>
> It's four experiments, not a dashboard. You create real Razorpay Test Mode
> payments, you decide which ones succeed, and the system responds to what you
> actually did. Nothing on that site is simulated.
>
> *Free tier — if it's been idle a while, the first load takes ~50s to wake up.*

---

## Why not just turn on auto-capture?

Because that's a checkbox, and Razorpay already ships it.

A system whose only lever is capture is a wrapper around a setting. The open
question is upstream, on **failures**: *what should happen next, and should
anything happen at all?*

Get it wrong in either direction and it costs money:

|  | What it costs you |
|---|---|
| **Doing nothing** | Recoverable revenue quietly evaporates |
| **A blind retry cron** | Re-charges cards reported stolen, hammers issuers that already declined permanently, and burns issuer trust chasing money that was never coming back |

What makes this hard is that `error_reason` is too coarse to act on. One value —
`payment_failed` — covers a temporary bank outage, a stolen-card block, and a
bare `"Payment failed."` Three failures, three different correct responses.
**The information separating them exists only in the issuer's free-text
description.**

---

## The receipt

Two runs over an identically-seeded 40-order backlog — **₹9,97,268.23 at risk**,
reproducible from a fixed seed. The only variable is whether the diagnosis layer
was available.

| Outcome | With diagnosis | No model (control) |
|---|---:|---:|
| Automated retry prompt | 19 · ₹7,35,923 · 73.8% | **0** |
| Stopped by a stopping rule | 15 · ₹1,96,241 · 19.7% | 2 · ₹16,968 · 1.7% |
| Escalated to a human | 6 · ₹65,104 · 6.5% | **38 · ₹9,80,300 · 98.3%** |

**Read the control column.** That is the safety argument, measured rather than
asserted: removing the model doesn't automate *less* — it automates **nothing**,
and hands 98.3% of the backlog to a human. The two stops that survive are
retry-budget cases, evaluated *before* the diagnosis is read, so they never
depended on it.

```bash
cd apps/api && python -m seed.synthetic_backlog <merchant_id> --orders 40
cd apps/api && python -m manual_run.run_recovery_batch <merchant_id> --source synthetic
```

---

## Where the AI is — and why it can't hurt you from there

The model classifies **why a payment failed**: a root cause, a class
(`TRANSIENT` / `TERMINAL` / `AMBIGUOUS`), a retry recommendation, a confidence.

**It decides nothing about money.** It is one input to a deterministic engine,
sitting behind three walls.

**① It doesn't know what's at stake.**
`FailureSignals` is a strict allowlist — no amount, no currency, no customer
identity, `extra="forbid"`. Passing an amount is an *error*, not a silent drop.
The model cannot be steered toward aggression on a large payment, because it
cannot tell a large payment from a small one.

**② It can't overrule a stop.**
The retry-budget check runs *before* the diagnosis is read at all. Rule order is
a security property here, not a style choice.

**③ Degrading it produces humans, not robots.**
No diagnosis, low confidence, `AMBIGUOUS`, or a self-contradictory result all
route to escalation. That is the control column above.

Enforced architecturally rather than by convention: `policy/`, `action/`,
`verification/` and `repository/` **cannot import the diagnosis layer at all**,
and only `diagnosis/` may import an LLM SDK. Tests fail the build if that stops
being true.

<details>
<summary><b>Where inference actually runs — read this before assuming</b></summary>

<br>

**This deployment does not call a language model at request time.** There is no
live inference.

Claude Opus 5 classified each failure-evidence pattern *offline* into
`datasets/diagnosis/failure_corpus.json`. `PrecomputedDiagnoser` serves those,
keyed on a fingerprint of the evidence, and **a cache miss escalates rather than
guessing**.

Persisted diagnoses carry `model_version = claude-opus-5/diagnosis_v1/offline`,
so a replay is distinguishable from a live call *in the database* — you don't
have to take this README's word for it.

`AnthropicDiagnoser` implements the identical protocol and calls the API live.
`--live-model` with an `ANTHROPIC_API_KEY` swaps it in and changes nothing else.

</details>

---

## Four experiments you can run yourself

The site is not a dashboard. Open it and pick one.

| | Experiment | What you actually do |
|---|---|---|
| **01** | *An authorized payment needs a decision* | Pick ₹500 or ₹8,000 and pay a real Test Mode order in Razorpay's own Checkout. **The amount is the experiment** — one side of the merchant's configured limit captures automatically, the other does not. |
| **02** | *Is the gateway having trouble?* | Counts what this merchant's recent payments actually did. Observed counts → arithmetic → interpretation, kept apart so the conclusion can be checked against its evidence. It reports concentration; it never claims an outage it cannot see. |
| **03** | *Is one payment failing, or many?* | Creates six real Test Mode orders and **freezes them as a group before any is paid** — so the denominator can't drift from "4 of 6" to "4 of 9" once results start landing. You decide which succeed. |
| **04** | *Does past behaviour change the decision?* | Pay more than once as the same payer. The history is real — Razorpay's payment object carries the email. History can send a payment **to a human for review; it can never buy it more automation.** |

> Creating an order moves no money. Only a human completing Checkout can produce
> a payment — which is exactly why the outcomes are worth analysing.

**Want to fail a payment on purpose?** In Razorpay's Checkout, enter an OTP
shorter than 4 digits. The site tells you which card numbers to use.

---

## Real vs synthetic, enforced in the database

Not by documentation — by a `CHECK` constraint. `recovery_batches.source` must be
`'razorpay_test_mode'` or `'synthetic'`, so there is no way to create a batch
that doesn't declare which. The label rides on the ledger as `money_is_real`, is
returned by the API, and renders as a banner **the UI has no code path to omit**.

Synthetic payments are only ever `failed`, and no failed payment can reach the
Razorpay write path — so the synthetic batch is *provably* free of external calls.

<details>
<summary><b>The one real transaction, verified end to end</b></summary>

<br>

| | |
|---|---|
| Order | `order_TXueleNMbhnp2s` |
| Payment | `pay_TXv6XNq04gxHpe` |
| Amount | ₹500 |
| Initial state | `authorized`, `captured=false` |
| Pipeline | `RECOMMEND_CAPTURE` → `ALLOW` → `CAPTURE_PAYMENT` → `VERIFIED_SUCCESS` |
| Confirmed by | an independent re-read of Razorpay: `captured=true` |
| Resolution | 3.9 seconds |

</details>

---

## Measuring recovery without lying

Two quantities, never merged — merging them is precisely how this number becomes
a lie.

- **Disposition** — a partition of the batch's frozen `revenue_at_risk` across
  mutually exclusive outcomes. Every paisa lands in exactly one bucket and the
  buckets sum to the total: *checked*, not assumed. The API returns
  `disposition_is_complete`, and the UI refuses to draw a breakdown that doesn't
  add up.
- **Verified recovery** — the sum of `outcome.recovered_amount` over captures
  that reached `VERIFIED_SUCCESS`, read from the action's own outcome.

Detection excludes orders already `paid`, and counts an order with three failed
attempts **once** — both enforced in SQL, because `revenue_at_risk` is the
denominator of every percentage reported here, and inflating it would flatter
everything downstream.

`STOPPED` is reported as *money no longer being pursued*. Never as money
recovered, or saved.

---

## Run it locally

```bash
cd apps/api && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
```

Create `apps/api/.env` — gitignored, and never commit it:

```
DATABASE_URL=postgresql://...
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
```

```bash
cd apps/api && python -m db.run_migrations
cd apps/api && .venv/Scripts/python -m uvicorn api.app:app --port 8010 --app-dir src
```

Frontend at **http://localhost:8010/** — one service serves the API *and* the
site.

```bash
cd apps/api && python -m pytest -q
```

**441 passing** against a live Postgres. Without `DATABASE_URL` the DB-backed
tests skip rather than fail.

<details>
<summary><b>Deploy your own</b></summary>

<br>

[`render.yaml`](render.yaml) is a Render Blueprint for the whole product — a
single web service, since the API also serves the frontend.

1. Render → **New** → **Blueprint** → connect this repo, branch `main`
2. Enter the three secrets it prompts for: `DATABASE_URL`, `RAZORPAY_KEY_ID`,
   `RAZORPAY_KEY_SECRET`. All are `sync: false`, so **none is ever stored in
   git**.
3. Deploy.

Migrations run in the build step and are idempotent — `run_migrations` records
what it has already applied — so a fresh database works with no manual step, and
a failed migration fails the build rather than booting a service whose schema
disagrees with its code. Python is pinned to 3.13 via `apps/api/.python-version`.

If Checkout won't open on your deployed domain, add that domain in your Razorpay
dashboard.

</details>

---

## Layout

| Path | |
|---|---|
| `apps/api/src/risk/` | revenue-at-risk detection + failure-pattern analysis (deterministic SQL) |
| `apps/api/src/diagnosis/` | **the AI layer** — the only package permitted to touch a model |
| `apps/api/src/intelligence/recovery_engine.py` | intervention selection (deterministic) |
| `apps/api/src/policy/` | merchant policy gate — authorizes independently of the recommendation |
| `apps/api/src/action/` | bounded execution, and **the only Razorpay write boundary** |
| `apps/api/src/verification/` | independent re-read of external state — the sole authority on success |
| `apps/api/src/context/customer_history.py` | payer history, identity fingerprinted rather than stored raw |
| `apps/api/src/observability/batch_ledger.py` | batch recovery ledger |
| `apps/api/src/pipeline/recovery.py` | the batch workflow |
| `apps/web/` | the four experiments — plain HTML/CSS/JS, no build step |
| `apps/api/tests/test_architecture_boundaries.py` | **every boundary above, mechanically enforced** |
| `docs/ARCHITECTURE.md` | full architecture, incl. §9 honest limitations |

---

## Limitations

Summarised. The full list is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §9,
and it is kept current rather than allowed to drift upward.

- The classifier is **unvalidated on real traffic**. The corpus is small and
  single-author, so its accuracy figures are self-consistent by construction and
  are *not* evidence of capability.
- **Inference is offline.** No model is called at request time in this deployment.
- **Only one real payment moved money.** Authorising a Test Mode payment needs a
  human in a browser, so scale is demonstrated synthetically — and labelled as
  such everywhere it appears.
- `CUSTOMER_RETRY_PROMPT` **has no external effect.** It stops at `AUTHORIZED`
  and is reported as `RETRY_PENDING`, never as a recovery. Wiring it up means
  Razorpay Payment Links, which was scoped out.
- **Escalation is a queue, not a notification.** It records an auditable outcome;
  it does not email anyone.
- **Webhooks are not implemented** — event ingestion is API-poll only.

---

<p align="center">
  <sub><b>Razorpay Buildathon · AI Revenue Recovery track</b><br>
  Every state the site shows comes from a real Razorpay operation, a real database record, or a real calculation.</sub>
</p>
