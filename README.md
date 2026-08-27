# Razorpay Contextual Merchant Decision Intelligence

Razorpay Hackathon — Open Track. A contextual decision-intelligence
layer that turns merchant transaction events and business context into
explainable, policy-bounded, verifiable actions, while measuring
whether those decisions actually create value.

## Status

**Repository foundation only.** No business logic, AI/ML, decision
engine, policy engine, Razorpay integration, database models, UI
features, or payment workflows have been implemented yet. This is
intentional — see `docs/adr/0001-repository-foundation.md`.

## Source of truth

Two governing documents live at the repo root:

- [`razorpay_contextual_decision_intelligence_architecture_contract_v0_1.md`](./razorpay_contextual_decision_intelligence_architecture_contract_v0_1.md)
  — **engineering authority.** Detailed architecture, contracts, and
  engineering decisions.
- [`razorpay_master_claude_code_handoff_v1.md`](./razorpay_master_claude_code_handoff_v1.md)
  — **consolidated project/process context.** Onboarding process,
  hackathon framing, Phase 1 verification status.

If these two documents ever conflict, that conflict must be raised and
resolved explicitly — never silently — before implementation proceeds.

## Repository layout

```text
apps/            web (Next.js) and api (FastAPI) applications — not yet scaffolded
packages/        shared contracts, ui, config — not yet populated
intelligence/    features, models, training, evaluation — not yet populated
workflows/       bounded action workflow definitions — not yet populated
infra/           infrastructure-as-code — empty until a deployment target is chosen
security/        security assessment reports — not yet performed
datasets/        synthetic merchant universe + held-out evaluation split — not yet generated
docs/adr/        architecture decision records
scripts/         operational scripts — none yet
tests/           unit and integration test structure — no framework chosen yet
docker/          per-service Dockerfiles — local stack defined in docker-compose.yml
```

## Local infrastructure

`docker-compose.yml` at the repo root defines Postgres + Redis for
local development. **As of this writing, Docker Desktop is not
installed on the primary development machine** — the compose file has
been written but not run or verified. See
`docs/adr/0001-repository-foundation.md` for details.

## Razorpay integration status

No Razorpay API/webhook integration exists yet. Capability claims used
to plan this project come from desk research plus a partial hands-on
verification pass — anything not backed by an actual captured
API/webhook response is marked **UNVERIFIED** in
`razorpay_capability_map_v0.1.md` (Phase 1 artifact) and must stay
that way until real evidence is collected. Never fabricate
verification.

## First vertical slice (planned, not yet built)

Payment-recovery scenario. Explicitly **not** a silent recharge of a
failed payment:

```text
payment failure
  → contextual analysis
  → AI recovery/opportunity assessment
  → policy gate
  → customer-facing recovery/retry intervention OR merchant recommendation
  → new payment attempt
  → verification
  → outcome
  → audit
```

Auto-capture of an already-authorized payment is a separate, narrower
action to be considered only once verified against real Razorpay
behavior and explicitly justified.
