# Architecture

**Razorpay Buildathon — AI Revenue Recovery track**

---

## 1. The problem this solves

When a payment fails, Razorpay tells the merchant it failed. It does not tell
them **which failures are worth chasing, what to do about each one, or when to
stop.**

That gap is expensive in both directions. Merchants who do nothing leave
recoverable revenue on the table. Merchants who run a blind retry cron re-charge
cards that were reported stolen, hammer issuers that have already declined
permanently, and burn issuer trust to recover money that was never recoverable.

The failed-payment backlog is revenue nobody owns. This system triages it.

### Why this is not "automatic payment capture"

Capture is the last step of the funnel and Razorpay already ships it as a
setting. A system whose only lever is capture is a wrapper around a checkbox —
its most sophisticated component decides whether to press a button Razorpay
presses for you.

The differentiated work is upstream, on failures, where the question is
genuinely open: *what should happen next, and should anything happen at all?*

---

## 2. The pipeline

```
Razorpay orders + payments
          |
          v
  Revenue risk detection          risk/detection.py          deterministic SQL
          |
          v
  AI diagnosis                    diagnosis/                 the ONLY model call
          |                                                  (classification only)
          v
  Intervention selection          intelligence/recovery_engine.py   deterministic
          |
          v
  Merchant policy gate            policy/                    deterministic
          |
          v
  Authorization                   action/ + DB trigger       fails closed
          |
          v
  Bounded action                  action/orchestrator.py     one attempt, ever
          |
          v
  Independent verification        verification/verifier.py   re-reads Razorpay
          |
          v
  Outcome -> audit + batch ledger observability/batch_ledger.py
```

Each stage is a separate package, and the boundaries between them are enforced
by tests in `tests/test_architecture_boundaries.py` rather than by convention.

---

## 3. Where the AI is, and why it is safe there

### What the model does

It classifies **why a payment failed**, from the failure evidence, into a closed
set: a root cause, a failure class (`TRANSIENT` / `TERMINAL` / `AMBIGUOUS`), a
retry recommendation, and a confidence.

That is a real judgement under uncertainty. Razorpay's `error_reason` is
structured but coarse — `payment_failed` is a catch-all covering temporary bank
outages, stolen-card blocks, and genuinely uninformative declines, which imply
completely different interventions. The distinguishing information exists only
in the issuer's free-text `error_description`.

`evaluation/diagnosis_harness.py` measures this. On the project's corpus, **five
`error_reason` values map to more than one failure class**, which costs the
*best possible* `error_reason` lookup table — one fitted on the answers
themselves, so no such table can do better — six unavoidable errors out of 24.
Those errors are not fixable by writing a better table.

> **Read the harness honestly.** The model's own score on that corpus is
> self-consistent by construction (the same author wrote the classifications and
> the ground-truth labels) and is *not* evidence of capability. The finding that
> survives is structural and checkable by inspection: the collisions are real,
> and a reason-code lookup cannot resolve them. Whether the classifier handles
> them well on production traffic is untested and would need a corpus this
> project does not have.

### Three properties that make it safe

These are the claims worth attacking first. Each has a test.

**1. The model is never told what a payment is worth.**
`diagnosis/signals.py` projects a payment row onto a strict allowlist that has
no amount, no currency, and no customer identity. `FailureSignals` is
`extra="forbid"`, so passing an amount is an error rather than a silently
dropped keyword. The model therefore cannot be steered into recommending more
aggressively for a large payment — it has no idea the payment is large.
*Test: `tests/test_diagnosis_signals.py`.*

**2. The model cannot override a stopping rule.**
In `RecoveryEngine.evaluate()`, the retry-budget check runs **before the
diagnosis is read at all**. A payment that has exhausted its budget stops no
matter how confidently the model recommends a retry. Rule order here is a
security property, not a style choice.
*Test: `test_retry_budget_stops_the_payment_even_when_the_model_is_maximally_confident`.*

**3. A degraded model produces more human review, never more automation.**
No diagnosis, low confidence, an `AMBIGUOUS` class, or a self-contradictory
diagnosis all route to `RECOMMEND_ESCALATION`. There is no path on which a
failing model widens what the system may do. A cache miss escalates rather than
falling back to the reason code.
*Test: `test_every_degraded_diagnosis_routes_to_human_escalation`.*

### Architectural containment

The packages that hold authority — `policy/`, `action/`, `verification/`,
`repository/` — **cannot import the diagnosis layer at all**, and only
`diagnosis/` may import an LLM SDK. Both are enforced mechanically. A model
output physically cannot reach a money decision without passing through the
deterministic engine that mediates it.

`diagnosis/` in turn imports no database driver and no Razorpay client, so it
can neither widen its own inputs beyond the allowlist nor act on anything.

### Where inference actually runs

**This deployment does not call a language model at request time.** Claude
Opus 5 classified each distinct failure-evidence pattern offline; those
classifications live in `datasets/diagnosis/failure_corpus.json` and are served
by `PrecomputedDiagnoser`, keyed on a fingerprint of the evidence.

Offline batch inference with cached online serving is an ordinary production
pattern — classifications are stable per evidence pattern, so recomputing one
per payment buys nothing. The point is that the claim is exact:

- Persisted diagnoses carry `model_version = claude-opus-5/diagnosis_v1/offline`.
  The `/offline` marker distinguishes a replay from a live call in the database.
- `AnthropicDiagnoser` implements the same `DiagnosisModel` protocol and calls
  the API live. `run_recovery_batch --live-model` with an `ANTHROPIC_API_KEY`
  swaps it in and changes nothing else. The seam is real, not decorative.

---

## 4. What is real and what is synthetic

The distinction is enforced by a database CHECK constraint, not by
documentation. `recovery_batches.source` must be `'razorpay_test_mode'` or
`'synthetic'`; there is no way to create a batch that does not declare which.
The label is carried on the ledger as `money_is_real`, returned by the API, and
rendered as a banner the UI has no code path to omit.

| | Real Test Mode batch | Synthetic batch |
|---|---|---|
| Payments | Genuine Razorpay objects | Generated locally, ids prefixed `pay_SYN` |
| Money movement | Real capture, independently verified | **None. Provably none** |
| External API calls | Yes | **None** |
| Demonstrates | That execution and verification are real | Triage, escalation and stopping at scale |

Synthetic payments are only ever `failed`, and no failed payment can reach the
Razorpay write path — the interventions it can select (retry prompt, escalate,
stop) are all internal. That is what makes "the synthetic batch is
side-effect-free" a structural fact rather than a promise.
*Test: `test_the_seeder_can_only_produce_failed_payments`.*

### The real transaction

| | |
|---|---|
| Order | `order_TXueleNMbhnp2s` |
| Payment | `pay_TXv6XNq04gxHpe` |
| Amount | ₹500 |
| Initial state | `authorized`, `captured=false` |
| Pipeline | `RECOMMEND_CAPTURE` → `ALLOW` → `CAPTURE_PAYMENT` → `VERIFIED_SUCCESS` |
| Independent confirmation | `captured=true`, re-read from Razorpay |
| Resolution time | 3.9 seconds |

---

## 4b. Measured results

Two runs over identically-seeded 40-order backlogs — **₹9,97,268.23 at risk**,
reproducible from a fixed seed on a given merchant. The only variable between
them is whether the diagnosis layer was available.

| Outcome | With diagnosis | No model (control) |
|---|---|---|
| Automated retry prompt | 19 · ₹7,35,923 · 73.8% | **0** |
| Stopped by a stopping rule | 15 · ₹1,96,241 · 19.7% | 2 · ₹16,968 · 1.7% |
| Escalated to a human | 6 · ₹65,104 · 6.5% | **38 · ₹9,80,300 · 98.3%** |
| Verified recovery | ₹0 | ₹0 |

Both runs are synthetic, so verified recovery is ₹0 in both — correctly. No
synthetic payment can reach the Razorpay write path, so none of this money could
move, and the ledger says so rather than reporting a recovery that did not
happen. Real money movement is the single Test Mode transaction in §4.

**The control column is safety property 3, measured.** Removing the model does
not make the system act more freely; it makes it act almost not at all, handing
98.3% of the money to a human. The two stops that survive without a model are
retry-budget cases — evaluated before the diagnosis is read, so they do not
depend on it. That is safety property 2 in the same table.

A worked example of property 2 from the primary run, showing the rule order in
the persisted reason codes:

```
Rs 15,997 | invalid_card | AI=TERMINAL conf=0.88 -> RECOMMEND_STOP
            reasons: ['RETRY_BUDGET_EXHAUSTED', 'PRIOR_ATTEMPTS:2', 'BUDGET:2']
```

The model classified this confidently, but the recorded reason is the budget
rule, not the diagnosis — the stopping rule resolved the case before the model's
output was consulted.

And a worked example of why reason codes alone are insufficient — the same
`error_reason`, two different interventions, separated only by the model's
confidence against the merchant's configured threshold:

```
Rs 46,943 | payment_cancelled | AI=TERMINAL  conf=0.88 -> RECOMMEND_STOP
Rs 29,412 | payment_cancelled | AI=TRANSIENT conf=0.66 -> RECOMMEND_ESCALATION
            reasons: ['LOW_DIAGNOSTIC_CONFIDENCE', 'CONFIDENCE:0.66', 'THRESHOLD:0.70']
```

---

## 5. Measuring recovery across a batch

`observability/batch_ledger.py` reports two quantities and never merges them,
because merging them is the usual way this number becomes a lie.

**The disposition** partitions the batch's frozen `revenue_at_risk` across
mutually exclusive outcomes — recovered, retry pending, approval required,
escalated, stopped, blocked, not yet processed. Every paisa lands in exactly one
bucket, and the buckets sum to the total. That sum is *checked*, not assumed:
`disposition_is_complete` is returned to the client, and the UI refuses to draw
a breakdown that does not add up.

**The verified recovery** is the sum of `outcome.recovered_amount` over captures
that reached `VERIFIED_SUCCESS` — money whose capture was confirmed by
re-reading Razorpay afterwards. It is read from the action's own outcome, never
derived from the detection-time estimate.

The amount at risk on an order and the amount captured on a payment are
different quantities; a single blended "recovered" figure would be
unfalsifiable. Reporting a complete partition plus a separately-sourced verified
total lets a reviewer check both independently.

**Denominator discipline.** Detection excludes orders already `paid` — some
later attempt succeeded, so nothing is at risk. Counting them would inflate
`revenue_at_risk`, which is the denominator of every recovery percentage. That
exclusion is enforced in SQL in `risk/detection.py`, not left to the caller.

**Not measured, deliberately:** whether recovery would have happened anyway, or
revenue "saved" by a stop. `STOPPED` is reported as money that was at risk and
is no longer being pursued — never as money recovered or saved.

---

## 6. What makes the workflow bounded

Every one of these is enforced somewhere specific:

| Bound | Where |
|---|---|
| Policy gates every intervention, including ones that cannot move money | `policy/orchestration.py` |
| An action cannot reach `AUTHORIZED`/`EXECUTING` unless its own persisted `policy_evaluation.allowed` is true | DB trigger, `0003_action_authorization_guard.sql` |
| Capture is attempted at most once per action — no retry loop exists | `action/orchestrator.py` |
| Idempotency is keyed on the real-world operation, so re-running a batch cannot double-capture | `domain/contracts.py` |
| Retry budget and terminal-failure stopping rules precede the model | `intelligence/recovery_engine.py` |
| Per-merchant limits come from `merchants.automation_limits`, failing closed on malformed config | `intelligence/recovery_orchestration.py` |
| `canonical_events` and `audit_entries` reject UPDATE and DELETE outright | DB triggers, `0001_init.sql` |

Running a batch is an operator action in the CLI, **not an HTTP endpoint** — it
costs model inference per payment and, on a real batch, can move money. Neither
belongs behind a button an anonymous visitor can press repeatedly. The API
exposes batch results read-only.

---

## 7. The audit trail

Append-only, enforced by database trigger, with an explicit ordering sequence
(`0004`) because `created_at` is fixed for a whole transaction and cannot
discriminate insertion order.

```
EVENT_INGESTED
  -> AI_DIAGNOSIS_RECORDED     what the model said, including when it failed
  -> DECISION_CREATED          what the deterministic engine decided
  -> POLICY_EVALUATED
  -> ACTION_AUTHORIZED / ACTION_BLOCKED / APPROVAL_PENDING
  -> ACTION_EXECUTED
  -> VERIFICATION_COMPLETED
```

`AI_DIAGNOSIS_RECORDED` is written **before** the decision and keyed to the
event, so the trail shows what the model said independently of what the system
then did with it — including cases where the deterministic layer overrode it. A
trail that only recorded final decisions would hide exactly the events a
reviewer most wants to see. The entry also records the precise evidence shown to
the model, so a reviewer can confirm from the audit trail alone that no amount
was passed to it.

---

## 8. Track criteria

| Criterion | Where |
|---|---|
| Detects revenue at risk | `risk/detection.py` — scans a merchant's payments, freezes the at-risk amount into a batch |
| Determines the right intervention | `intelligence/recovery_engine.py` — four distinct executable interventions, selected deterministically from an AI diagnosis |
| Executes a bounded recovery workflow | `pipeline/recovery.py` — §6 above |
| Measured money recovered across a batch | `observability/batch_ledger.py` — §5 above |
| Compliant escalation | `ESCALATE_TO_MERCHANT`, policy-gated and audited like any other action |
| Stopping rules | Retry budget and terminal-failure rules, both evaluated before the model |
| Audit trail | §7 above |

---

## 9. Honest limitations

Stated here rather than left to be discovered.

- **The classifier is unvalidated on real traffic.** The corpus is small,
  single-author, and its accuracy figures are self-consistent by construction.
  Only the structural claim about reason-code collisions survives scrutiny.
- **Inference is offline.** No language model is called at request time in this
  deployment. The live path exists and is exercised by the same protocol, but
  has not been run at scale.
- **Only one real payment moved money.** Authorising a Razorpay Test Mode
  payment needs a human to complete Checkout in a browser, so a real batch of
  forty was not achievable. Scale is demonstrated synthetically and labelled as
  such throughout.
- **`CUSTOMER_RETRY_PROMPT` has no external effect.** It stops at `AUTHORIZED`
  and dispatches nothing — there is no customer-facing channel wired up. It is
  reported as `RETRY_PENDING`, never as a recovery. Making it real would mean
  Razorpay Payment Links, which was scoped out.
- **Escalation is a queue, not a notification.** `ESCALATE_TO_MERCHANT` records
  an auditable outcome; it does not email anyone.
- **The synthetic failure mix is not calibrated** to any real merchant's
  distribution. Archetypes are sampled uniformly, for coverage of decision paths
  rather than realism of proportions.
- **Webhooks are not implemented.** Event ingestion is API-poll only.

---

## 10. The four guided problem journeys

The site is not a dashboard. It is four experiments a visitor runs
themselves, each one ending in a state this system actually observed.

| Problem | What the visitor does | What is real |
|---|---|---|
| 01 · An authorized payment needs a decision | Picks an amount, pays a real Test Mode order, runs the pipeline | Real order, real Checkout, real decision/policy/action/verification |
| 02 · Is the gateway having trouble? | Reads this merchant's recent payment outcomes | Real aggregation over observed payments |
| 03 · One payment or many? | Creates six real orders, drives each outcome | Real orders, real payments, cohort frozen at creation |
| 04 · Does customer history change the decision? | Pays repeatedly as the same payer | Real identity from the stored Razorpay payment object |

### Real Razorpay Checkout

`provisioning/razorpay_order_client.py` creates orders through Razorpay's
Orders API with `payment_capture = 0` pinned to a module constant. That
parameter is load-bearing: without it Razorpay auto-captures on payment and
there is no capture decision left to make. An earlier order in this
project's history was created without it and reached `captured` before
reconciliation ever saw it -- the fix lives at the only place that can
prevent it.

The browser opens Razorpay's own Checkout, loaded from Razorpay's domain,
using the **publishable** key served by `GET /checkout-config`. That
endpoint never reads the secret, and refuses to serve a key that is not
`rzp_test_` -- a live key in a browser would let a visitor start real
payments. Checkout's own callbacks are treated as a signal to go and ask
the server, never as evidence that money moved.

### Two new deterministic read layers

`risk/failure_patterns.py` answers problems 02 and 03. It keeps three
things in separate types, because the credibility of the conclusion rests
on a reader being able to check it against its evidence:

```
ObservedFacts    row counts, nothing inferred
ComputedSignals  arithmetic on those counts
Interpretation   what it might mean, plus what it cannot know
```

The strongest thing it will ever say is that failures are *concentrated*,
which is consistent with a wider problem. It will not say the gateway is
down, because one merchant's own payment rows cannot establish that, and
every report carries that limitation in its own payload.

`context/customer_history.py` answers problem 04, from data the system was
already storing: Razorpay's payment object carries the payer's email, and
`payment_attempts.raw_reference` has held it since migration 0001. Counts
of prior outcomes are added to the ContextSnapshot as DERIVED fields, keyed
on a **fingerprint** rather than the address -- `decisions.context_snapshot`
is persisted on every decision and there is no reason to copy an email into
it. A payment with no identity produces no fields, which is the honest
answer for a synthetic row rather than an invented empty profile.

### What history is allowed to do

The customer-history rule sits at exactly one point in `RecoveryEngine`:
the branch that would otherwise produce an **automated retry**. It is
reached only after every stopping rule and every escalation has already
been decided, so it cannot reverse a stop, cannot rescue an escalation into
automation, and cannot fire unless the system was about to act on its own.
The only transition it can cause is automation → human review.

A payer with a strong history gets a reason code recorded on the decision
and no additional authority. That asymmetry is the point: a payment's past
can buy it more scrutiny, never more freedom.
*Tests: `tests/test_customer_history.py`.*

### Cohort denominators

`payment_experiments` freezes which orders belong to an experiment **before
any of them is paid**. Reconstructing the group later from "recent orders"
would let the denominator move as outcomes arrived, so "4 of 6" would stop
meaning four of those six. Orders nobody paid are reported as unpaid rather
than dropped, for the same reason -- dropping them would turn 4 of 6 into 4
of 4 and convert a measured result into a much stronger claim.
