# Phase 1 Hands-On Verification Report

**Status:** Steps 1–9 VERIFIED (real Test Mode evidence observed and reported
by the project owner). Steps 10–13 (webhooks) UNVERIFIED/BLOCKED — local
tunnel tooling (webhook.site hostname rejected by Razorpay, zrok account
enablement returned server-side HTTP 500) could not be resolved within
reasonable time and tooling work was deliberately stopped per explicit
instruction rather than continued indefinitely.

Every claim below is labeled:
- **VERIFIED** — directly observed in this project's Test Mode evidence.
- **DOCUMENTED** — stated in Razorpay's public documentation, but not
  independently observed by this project. Treat as provisional.
- **UNVERIFIED** — neither observed nor confirmed; do not assume.

No fabricated observations appear anywhere in this document.

---

## A. Actual Capability Matrix

| Capability | Status | Evidence |
|---|---|---|
| Test Mode key authentication | **VERIFIED** | Step 1: HTTP 200 on `GET /v1/orders` |
| Create Order | **VERIFIED** | Step 2: `order_TUtsn4abstMa1L`, `order_TV4zd1gEZHQRZ7` created successfully |
| Fetch Order | **VERIFIED** | Step 3, Step 7: order fetched, fields match docs (`status`, `amount_paid`, `amount_due`, `attempts`) |
| Order aggregate state after mixed fail/success attempts | **VERIFIED** | Step 7: `amount_paid: 50000`, `amount_due: 0`, `status: paid`, `attempts: 3` after 2 failures + 1 success |
| Multiple distinct payment objects per order (non-linear event sequence) | **VERIFIED** | Step 7: 3 distinct `pay_...` IDs returned under one order, not one mutated payment |
| Payment failure with `error_source`/`error_step`/`error_reason` | **VERIFIED** | Step 4–6: customer-side cancellation (`error_source: customer`, `error_step: payment_authentication`, `error_reason: payment_cancelled`) and gateway-side failure (`error_source: gateway`, `error_step: payment_authorization`, `error_reason: payment_failed`) both observed as genuinely distinct failure shapes |
| `method` field value: `card` | **VERIFIED** | Steps 4–6, 8: all 3 payment objects |
| `method` field values: `upi`, `netbanking`, `wallet`, `emi` | **UNVERIFIED** | Not attempted this pass; capability map's "broader ecosystem" claim remains unconfirmed for this account |
| Manual capture (`payment_capture: 0`) actually withholds auto-capture | **VERIFIED** | Step 9: payment landed in `authorized`, `captured: false`, before the capture call |
| `POST /v1/payments/:id/capture` (authorized → captured) | **VERIFIED** | Step 9: HTTP 200, `status: captured`, `captured: true` after capture call with matching amount |
| Checkout success callback shape (`razorpay_payment_id`, `razorpay_order_id`, `razorpay_signature`) | **VERIFIED** | Steps 4–6: callback observed for `pay_TUu3sDIxP8DbTn` |
| Checkout-callback signature (`razorpay_signature`) validity | **UNVERIFIED** | Signature was observed present in the callback; it was not independently HMAC-verified against the order/payment ID pair in this pass — only the *webhook* signature script (`verify_webhook_signature.py`) was prepared, and no webhook was received to run it against |
| Webhook delivery (any event) | **UNVERIFIED / BLOCKED** | No webhook has been received by any endpoint this project controls |
| Webhook envelope shape (`account_id`, `event`, `contains[]`, `payload{}`, `created_at`) | **DOCUMENTED** | Per Razorpay webhook docs; not observed |
| `X-Razorpay-Signature` HMAC-SHA256 over raw body | **DOCUMENTED** | Per docs and `verify_webhook_signature.py`'s design; mechanism never exercised against a real payload |
| `x-razorpay-event-id` header for dedup | **DOCUMENTED** | Per docs; never observed |
| Webhook ordering not guaranteed | **DOCUMENTED** | Per docs; no ordering data collected |
| Webhook duplicate delivery / resend behavior | **UNVERIFIED** | No webhook received, so no resend could be tested |
| Payment Links (Test Mode) | **UNVERIFIED** | Deferred by explicit choice — not attempted this pass |
| Refund entity/create-refund behavior | **UNVERIFIED** | Not attempted this pass |
| Customer entity (profile-only, no history) | **DOCUMENTED** | Per capability map desk research; not independently re-verified this pass (not required for the verified scope above) |

---

## B. Actual Order / Payment-Attempt State Machine

Based only on VERIFIED evidence (steps 1–9):

```text
ORDER (order_TUtsn4abstMa1L)
  status: created  →  [not directly observed mid-sequence]  →  status: paid
  amount_due: 50000 → amount_due: 0
  amount_paid: 0    → amount_paid: 50000
  attempts: 0       → attempts: 3

  ├── PAYMENT ATTEMPT 1 (pay_TUtxYgO6QIV9ZQ)
  │     created → failed
  │     captured: false
  │     error_source: customer, error_step: payment_authentication,
  │     error_reason: payment_cancelled
  │
  ├── PAYMENT ATTEMPT 2 (pay_TUtz3K7s6ab3e4)
  │     created → failed
  │     captured: false
  │     error_source: gateway, error_step: payment_authorization,
  │     error_reason: payment_failed
  │
  └── PAYMENT ATTEMPT 3 (pay_TUu3sDIxP8DbTn)
        created → captured
        captured: true
```

Separately, the manual-capture order confirms a second branch of the payment
state machine:

```text
ORDER (order_TV4zd1gEZHQRZ7, payment_capture: 0)
  └── PAYMENT (pay_TV530e8hTSjpC8)
        created → authorized (captured: false)
        → [POST /v1/payments/:id/capture, amount-matched]
        → captured (captured: true)
```

**Confirmed:** the "correct model" from the architecture contract (§9) —
independent payment-attempt objects under one order, order-level aggregate
state distinct from payment-attempt state — is exactly what was observed.
No mutation of a single payment object across failure/success was seen;
each attempt is a genuinely separate entity.

**Not directly observed (gap, not contradiction):** the order's `status`
value *during* the failed-attempt-but-not-yet-succeeded window (docs claim
an intermediate `attempted` status). The order was only fetched before any
attempts (`created`) and after all three (`paid`) — the middle state was
never independently fetched. Mark `attempted` as **DOCUMENTED**, not
VERIFIED, until a fetch is taken between a failure and the eventual
success.

---

## C. Webhook Contract

**Everything in this section is DOCUMENTED, not VERIFIED.** No webhook has
been received by any endpoint this project controls. Treat all of the
below as the *design assumption* to build against defensively, not as
confirmed behavior.

```text
POST <configured endpoint>
Headers:
  X-Razorpay-Signature: <hex HMAC-SHA256 of raw body, keyed by webhook secret>
  x-razorpay-event-id: <dedup key>
Body (JSON):
  {
    "account_id": "...",
    "event": "payment.failed" | "payment.authorized" | "payment.captured" |
              "order.paid" | "refund.*" | ...,
    "contains": ["payment"] | ["order", "payment"] | ...,
    "payload": { "payment": { "entity": {...} }, "order": { "entity": {...} } },
    "created_at": <unix timestamp>
  }
```

Contract rules to build against (per docs, unverified):
- Ordering is **not guaranteed** — a handler must not assume
  `payment.failed` always arrives before a later `payment.captured` for the
  same order.
- Delivery may be **duplicated** — dedupe on `x-razorpay-event-id` before
  processing.
- The signature must be computed over the **raw, unparsed** request body —
  a common integration bug is running HMAC after a JSON-parsing middleware
  has already re-serialized the body.
- A webhook must **never** be trusted alone for a critical state
  transition — always confirm via an authoritative `GET` fetch (this
  principle is already validated independently: the actual `GET
  /v1/orders/:id/payments` and `GET /v1/orders/:id` calls in steps 7 and 9
  are VERIFIED to return authoritative, correct state).

**Until a real webhook delivery is captured, no code should assume the
exact envelope field names above are byte-correct** — build the ingestion
layer to fail loudly (not silently) on an unexpected shape, rather than
assuming it will match this table.

---

## D. First Vertical-Slice Recommendation

Given what's actually VERIFIED:

- Multiple payment attempts under one order is real and observable via
  **API fetch**, not just webhooks.
- Capture of an authorized payment is a real, working bounded write
  action.
- Webhook delivery is currently unavailable in the local dev environment.

**Recommendation: build the first vertical slice's event source on API
polling/fetch, not webhook delivery, for local development and the demo
environment.** This is not a design compromise — the architecture already
treats webhooks as non-authoritative signals requiring API verification
(architecture contract §6, §17); it's simply building the *verification*
half of that pattern first, and treating the webhook half as an
accelerant to be added once a real public endpoint exists (e.g. an actual
staging deployment with a real domain, which sidesteps the local-tunnel
blocker entirely — no webhook.site/ngrok/zrok involved).

Concrete flow for Scenario A, revised to not depend on webhook delivery
yet:

```text
Periodic/post-checkout API fetch of order + its payments
  → latest attempt is `failed`, order not yet `paid`
  → contextual analysis (customer/value signal, failure error_source/step/reason)
  → AI recovery/opportunity assessment
  → policy gate
  → customer-facing retry prompt / payment-link, OR merchant recommendation
    (never a silent server-side recharge — confirmed impossible via the
    Payments API regardless, and already the agreed product direction)
  → new payment attempt appears as a new payment object under the same order
    (VERIFIED mechanism)
  → verification via authoritative GET fetch (VERIFIED mechanism)
  → outcome
  → audit
```

The now-VERIFIED capture flow (authorized → captured) is a separate,
narrower, also-viable bounded action (e.g. auto-capturing a
manual-capture-authorized payment before it expires) — it is no longer
blocked on verification, but including it in v1 scope vs. deferring it to
a later scenario is still an open product decision, not an engineering
one.

## E. Implementation Blockers

1. **Webhook-triggered flows cannot be developed or tested against real
   Razorpay webhook deliveries in the current local environment.**
   webhook.site is hostname-blocked by Razorpay; zrok account enablement
   fails server-side (HTTP 500) independent of anything on this end.
   Mitigation: build and test the API-polling path first (see D); revisit
   webhooks once a real publicly-reachable staging endpoint exists.
2. **Non-card `method` values are unverified.** The context/decision layer
   must not hardcode behavior for `upi`/`netbanking`/`wallet`/`emi` as if
   confirmed — treat as unverified until independently observed.
3. **Checkout-callback signature verification is unverified.** Only the
   webhook-signature script has been prepared/tested for MATCH/MISMATCH
   logic; the client-side `razorpay_signature` in the success callback has
   not been independently HMAC-verified in this project yet.
4. **Payment Links and Refunds are entirely unattempted.** Not a blocker
   for the current scope, but should not be assumed available until
   tested, if either becomes relevant to a later scenario.
5. **The order's intermediate `attempted` status was never fetched
   mid-sequence** — a minor gap, cheap to close in a future pass (fetch
   the order between a failure and the next attempt).

## F. Architecture-Change Log

| Old assumption | Observed reality | Required change | Reason |
|---|---|---|---|
| Local webhook testing would be straightforward via webhook.site per the runbook | Razorpay rejects the webhook.site hostname outright; zrok enablement fails server-side | No change to the core architecture (webhooks were already designed as non-authoritative). **Sequencing change**: build/verify the API-fetch/reconciliation path first; defer webhook ingestion until a real public endpoint is available | Avoids blocking the entire first vertical slice on tunnel tooling; consistent with the architecture's existing "webhook is not truth" principle |
| "ATTEMPT_RECOVERY" could plausibly be read as re-charging the customer's card | Confirmed experimentally: each new attempt is a genuinely distinct payment object; the Payments API has no server-initiated charge capability at all | None needed — the prior decision (recovery = customer-facing retry/prompt or recommendation, never silent recharge) is now independently confirmed correct by real evidence, not just doc-inferred | Real evidence now backs a decision that was previously precautionary |
| Capture was a *speculatively* bounded write action (capability map: "the one clearly-bounded write action") | Capture works exactly as documented: `authorized → captured`, amount-matched, respects `payment_capture: 0` | Promote capture from DOCUMENTED to VERIFIED in the capability matrix; it can now be treated as a validated candidate for a Level-3 bounded action once policy limits are defined | Removes a prior "assume it probably works" caveat |
| `method` enum plausibly includes upi/netbanking/wallet/emi based on general Razorpay ecosystem knowledge | Only `card` has been observed on this account this pass | Decision/context engine must treat non-card methods as unverified, not merely "less common" | Prevents building method-specific logic on an unconfirmed assumption |
| Webhook envelope/header shape could be treated as effectively settled since it's official documentation | Zero real webhook deliveries captured to date | Webhook contract (Section C) stays explicitly labeled DOCUMENTED, not VERIFIED, in every downstream artifact until a real delivery is captured; ingestion code should fail loudly on an unexpected shape rather than silently assuming correctness | Prevents silent drift from documentation-derived assumptions into treated-as-fact architecture |

---

**Next open items, unchanged from before:** GitHub remote still not
configured; Docker Desktop still not installed/verified; webhook evidence
still pending a real public endpoint. No repository code, schema, or
business logic was added as part of this report.
