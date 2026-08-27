# Razorpay Contextual Merchant Decision Intelligence

## Architecture Freeze + Engineering Contracts v0.1

**Project type:** Razorpay Hackathon --- Open Track\
**Primary thesis:** Contextual Merchant Decision Intelligence\
**Status:** Architecture baseline / pre-implementation\
**Time constraint:** 9-day build window\
**Source of truth:** This document is the engineering contract for the
current project direction.

------------------------------------------------------------------------

# 1. Executive Thesis

Razorpay already sits close to a merchant's most important transactional
events: orders, payments, failures, captures, refunds and related
payment-state changes.

The project is **not** another generic AI chatbot, another dashboard,
five disconnected agents, or a meta-agent that coordinates the four
official tracks.

The project is a **contextual decision-intelligence layer for
merchants**.

The system continuously moves through:

> **EVENT → CONTEXT → EXPECTATION → DECISION → POLICY GATE → ACTION →
> VERIFICATION → AUDIT → FEEDBACK**

The merchant should not need to understand the internal agents.

The merchant should experience:

1.  What is happening?
2.  Why does it matter?
3.  What should I do?
4.  Why are you recommending this?
5.  What are you allowed to do automatically?
6.  What requires my approval?
7.  What happened after the action?
8.  What evidence supports the system's performance?

The product should feel like Razorpay is giving a merchant a **business
decision brain**, not merely exposing payment infrastructure.

------------------------------------------------------------------------

# 2. Product Philosophy

## 2.1 The Jethalal model

Use a small, high-value electronics merchant as the concrete mental
model.

Jethalal:

-   sells high-value electronics;
-   purchases inventory from upstream suppliers;
-   has repeat customers and first-time customers;
-   depends on steady cash flow;
-   wants business growth;
-   must maintain customer trust;
-   must maintain supplier relationships;
-   has a reputation to protect;
-   needs control over what is actually happening in the business;
-   cannot know the future with certainty.

The generic merchant model is identical in structure:

**Merchant + Customers + Orders + Payments + Inventory + Suppliers +
External Context + Business Objectives**

Jethalal is a narrative anchor, not a hard-coded persona.

------------------------------------------------------------------------

# 3. Five Merchant Objectives

The system should model five competing dimensions rather than optimizing
only "growth":

1.  **Survival**
    -   cash-flow stability;
    -   prevention of avoidable loss;
    -   protection against dangerous decisions.
2.  **Trust**
    -   reliable customer interactions;
    -   reliable supplier relationships;
    -   correct payment/order state.
3.  **Reputation**
    -   consistency of merchant behavior;
    -   avoiding preventable customer failures;
    -   maintaining service quality.
4.  **Control**
    -   knowing what is actually happening;
    -   reducing uncertainty;
    -   having a traceable explanation for every consequential decision.
5.  **Growth**
    -   increased conversion;
    -   recovered revenue;
    -   better inventory decisions;
    -   higher customer lifetime value.

**Important:** Peace of mind is treated as an outcome of control,
reliability and predictable operations rather than as a direct
optimization variable.

The decision engine must explicitly account for trade-offs between these
objectives.

------------------------------------------------------------------------

# 4. Non-Negotiable Hackathon Bar

The project must satisfy the following:

-   Real problem.
-   Working product.
-   Meaningful AI.
-   Evidence of value.
-   Explainable consequential decisions.
-   Bounded actions.
-   Gated money-related actions.
-   Explicit stopping rules.
-   Compliant escalation.
-   Graceful failure handling.
-   Complete audit trail.
-   Evaluation on a held-out test set.
-   No cherry-picked single example as proof.
-   Reliability and depth strong enough for technical questioning.

A feature is not justified because it makes the architecture look
larger.

A feature is justified only if it materially improves:

-   decision quality;
-   business value;
-   safety;
-   reliability;
-   explainability;
-   evidence;
-   or differentiation.

------------------------------------------------------------------------

# 5. Core Architecture

``` mermaid
flowchart TD
    A[Razorpay APIs + Test Mode] --> B[API/Webhook Ingestion]
    W[Razorpay Webhooks] --> B
    E[Merchant Inputs / Synthetic Context] --> B

    B --> C[Event Validation + Normalization]
    C --> D[Event Store / Operational DB]
    C --> Q[Async Event Queue]

    Q --> F[Context Engine]
    D --> F
    X[External/Synthetic Context] --> F

    F --> G[Feature & State Layer]
    G --> H[AI / ML Intelligence Layer]

    H --> H1[Demand / Opportunity Signals]
    H --> H2[Risk / Anomaly Signals]
    H --> H3[Customer / Transaction Signals]
    H --> H4[Outcome / Impact Estimation]

    H1 --> I[Decision Engine]
    H2 --> I
    H3 --> I
    H4 --> I
    F --> I

    I --> J[Policy + Constraint Engine]
    J --> K{Action Allowed?}

    K -->|No| L[Escalate / Recommend / Stop]
    K -->|Approval Required| M[Human Approval Gate]
    K -->|Allowed| N[Action Orchestrator]
    M --> N

    N --> R[Razorpay Test APIs / Merchant Workflows]
    N --> S[Notification / External Workflow]

    R --> T[Verification Engine]
    S --> T

    T --> U[Outcome + Feedback Store]
    U --> F

    I --> V[Explanation Engine]
    J --> V
    N --> V
    T --> V

    V --> Z[Audit Trail]
    T --> Z
    B --> Z

    Z --> AA[Merchant Decision UI]
    U --> AB[Evaluation / Analytics]
```

------------------------------------------------------------------------

# 6. Architectural Layers

## Layer A --- Razorpay Integration

### Responsibility

Consume and interact with Razorpay test-mode capabilities where the
project actually needs them.

### Inputs

Potentially relevant Razorpay entities/events include:

-   Orders
-   Payments
-   Payment status
-   Payment failures
-   Payment captures
-   Refunds
-   Order-paid events
-   Customer/payment-associated information available through the
    integration
-   Webhook events

Razorpay documentation confirms that Test Mode uses test API keys and
simulates transactions without moving real money. Razorpay's API uses
authenticated REST endpoints, and payment/order state changes can be
consumed through webhooks.

### Contract

The application must never assume that a webhook alone is the final
truth for a critical state.

For critical verification:

**Webhook event → validate → fetch/verify state when required → update
internal state**

------------------------------------------------------------------------

# 7. Data Ingestion Layer

## Components

-   Razorpay API connector
-   Webhook receiver
-   Signature/authentication validation
-   Event schema validator
-   Event deduplication
-   Event normalization
-   Idempotency layer
-   Dead-letter/error handling

## Input contract

Every external event must become an internal canonical event.

Example:

``` json
{
  "event_id": "evt_x",
  "source": "razorpay",
  "event_type": "payment.failed",
  "occurred_at": "timestamp",
  "merchant_id": "merchant_x",
  "entity_id": "pay_x",
  "order_id": "order_x",
  "payload_version": "v1",
  "raw_reference": "secure-storage-reference"
}
```

Do not couple the entire intelligence layer directly to Razorpay's raw
payload format.

------------------------------------------------------------------------

# 8. Event State Model

Canonical events should include at minimum:

### Payment lifecycle

-   payment.authorized
-   payment.captured
-   payment.failed

### Order lifecycle

-   order.created
-   order.paid
-   order.pending
-   order.expired/cancelled where applicable

### Refund lifecycle

-   refund.created
-   refund.processed
-   refund.failed

The exact event catalog must be confirmed against the current Razorpay
documentation before implementation.

### Critical state rule

The system must support non-linear event sequences.

Example:

``` text
payment.failed
       ↓
customer retries
       ↓
payment.captured
```

A `payment.failed` event must therefore not automatically be treated as
terminal.

Razorpay explicitly documents cases where a failed event can be followed
by a successful capture, including late authorization and retries.

------------------------------------------------------------------------

# 9. Context Engine

The context engine is one of the project's main differentiators.

Raw events are not enough.

The context engine converts events into merchant-relevant state.

## Context categories

### Merchant context

-   merchant type
-   business category
-   region
-   operating hours
-   objectives
-   automation limits
-   risk tolerance
-   inventory constraints
-   cash-flow constraints

### Customer context

-   first-time vs repeat
-   historical order count
-   historical spend
-   average order value
-   payment success/failure history
-   refund behavior
-   recency
-   customer segment

### Transaction context

-   amount
-   order age
-   payment method
-   failure reason
-   retry count
-   product category
-   order state
-   historical baseline

### Product context

-   category
-   price
-   inventory
-   stock velocity
-   margin
-   demand trend

### External context

Potentially synthetic or mocked for the prototype:

-   seasonality
-   festivals
-   regional demand
-   weather
-   supplier lead time
-   market conditions

External context must be clearly labeled as external/synthetic rather
than falsely presented as native Razorpay data.

------------------------------------------------------------------------

# 10. Expectation Engine

The system needs a concept of:

> "What normally should have happened?"

Examples:

-   A repeat customer normally completes a purchase.
-   A certain payment method normally succeeds for this merchant.
-   A product normally sells at a certain rate.
-   A payment failure normally recovers within a certain number of
    attempts.
-   Inventory normally covers a certain number of days.

The expectation engine provides the baseline against which unusual
states are detected.

This prevents the AI from treating every event as an anomaly.

------------------------------------------------------------------------

# 11. AI / ML Layer

The AI layer must have meaningful responsibility.

It should not exist merely to generate natural-language explanations.

Possible model families:

### A. Demand / opportunity prediction

Possible models:

-   XGBoost
-   LightGBM
-   Prophet where appropriate
-   baseline statistical models

### B. Anomaly / risk detection

Possible models:

-   Isolation Forest
-   One-Class methods
-   supervised classifiers where labeled synthetic ground truth exists

### C. Customer / transaction scoring

Possible models:

-   logistic regression baseline
-   XGBoost
-   calibrated classifier

### D. Decision ranking

Possible approaches:

-   weighted objective scoring
-   learning-to-rank
-   contextual scoring
-   constrained optimization

### E. LLM

LLM responsibility should be constrained to tasks where language
reasoning is genuinely useful:

-   interpreting merchant intent;
-   converting structured evidence into explanations;
-   generating human-readable recommendations;
-   reasoning over structured decision context;
-   handling natural-language merchant questions.

The LLM must not independently bypass the policy engine.

------------------------------------------------------------------------

# 12. Decision Engine

The decision engine is the heart of the product.

It receives:

``` text
Merchant State
+
Current Event
+
Derived Context
+
Expectation
+
AI Signals
+
Business Objectives
+
Constraints
```

It outputs:

``` text
Decision
+
Priority
+
Confidence
+
Expected Impact
+
Evidence
+
Required Action
+
Authority Level
+
Expiration
```

Example:

``` json
{
  "decision": "ATTEMPT_RECOVERY",
  "priority": "HIGH",
  "confidence": 0.87,
  "expected_impact": {
    "revenue_recovered": 42000
  },
  "reason_codes": [
    "HIGH_CUSTOMER_VALUE",
    "RECOVERABLE_FAILURE_PATTERN",
    "LOW_RISK"
  ],
  "authority": "AUTOMATED",
  "expires_at": "timestamp"
}
```

------------------------------------------------------------------------

# 13. Policy & Constraint Engine

This component must be independent from the LLM.

It enforces:

-   monetary limits;
-   retry limits;
-   action frequency limits;
-   merchant-specific policies;
-   category restrictions;
-   risk thresholds;
-   approval requirements;
-   time restrictions;
-   escalation requirements;
-   stopping rules.

### Fundamental rule

**The AI recommends. The policy engine authorizes.**

The LLM must never directly decide that an unrestricted financial action
is permissible.

------------------------------------------------------------------------

# 14. Action Authority Levels

Every action must have an authority level.

### LEVEL 0 --- Observe

No action.

### LEVEL 1 --- Recommend

AI recommends; merchant decides.

### LEVEL 2 --- Prepare

System prepares an action but requires approval.

### LEVEL 3 --- Bounded Automatic Action

System executes within predefined limits.

### LEVEL 4 --- Forbidden

System cannot execute the action automatically.

This gives us a clean explanation during the demo:

> "The AI wanted to do X, but the policy engine prevented it because the
> action exceeded the merchant's automation boundary."

That is a feature, not a failure.

------------------------------------------------------------------------

# 15. Stopping Rules

Every automated workflow must have an explicit termination condition.

Example:

``` text
retry_count >= max_retries
OR
risk_score >= threshold
OR
action_window_expired
OR
customer_declined
OR
merchant_policy_disallows_action
OR
outcome_verified
```

Never create an agentic loop with unlimited retries.

------------------------------------------------------------------------

# 16. Action Orchestrator

Responsible for executing an approved decision.

Responsibilities:

-   idempotency;
-   action authorization;
-   API calls;
-   retries;
-   timeout handling;
-   state transitions;
-   compensation;
-   result recording.

It should not make new business decisions.

------------------------------------------------------------------------

# 17. Verification Engine

Every consequential action must have a verification step.

Example:

``` text
Action:
Attempt payment recovery

Verification:
Did payment become captured?

YES → SUCCESS
NO  → continue only if policy allows
NO  → otherwise stop/escalate
```

The system must never report success merely because an API request
returned successfully.

------------------------------------------------------------------------

# 18. Audit & Explainability Engine

Every consequential decision produces an immutable decision trace.

Minimum audit fields:

-   event ID
-   merchant ID
-   decision ID
-   timestamp
-   input signals
-   derived context
-   model version
-   model score
-   decision
-   policy version
-   policy result
-   approval status
-   action
-   API response reference
-   verification result
-   final outcome

### Explanation structure

The UI should answer:

**Why this?**

**Why now?**

**Why this merchant?**

**Why this action?**

**Why not the alternatives?**

**What evidence supported the decision?**

**What constraints were applied?**

------------------------------------------------------------------------

# 19. Graceful Failure Requirement

At least one failure scenario must be deliberately demonstrated.

Recommended scenario:

``` text
Payment failure
      ↓
AI identifies recovery opportunity
      ↓
Policy permits one bounded action
      ↓
Action fails / webhook delayed
      ↓
Verification detects uncertainty
      ↓
System does NOT blindly retry
      ↓
Escalates / waits / fetches authoritative state
      ↓
Audit trail records the complete sequence
```

This should be part of the final demo.

------------------------------------------------------------------------

# 20. Evaluation Architecture

Do not evaluate on one hand-picked merchant.

Create a synthetic merchant universe.

Recommended initial target:

-   50+ merchants
-   multiple merchant categories
-   multiple customer segments
-   hundreds/thousands of transactions
-   normal and abnormal payment patterns
-   multiple failure types
-   inventory conditions
-   seasonal conditions
-   known ground truth
-   injected edge cases

## Dataset split

``` text
Historical / Training
        ↓
Validation
        ↓
HELD-OUT TEST SET
```

The final held-out set must remain untouched during model tuning.

------------------------------------------------------------------------

# 21. Evidence of Value

Depending on the implemented scenarios, measure:

### Revenue

-   recovered revenue
-   prevented revenue loss
-   conversion improvement
-   average order value

### Decision quality

-   precision
-   recall
-   false-positive rate
-   false-negative rate
-   calibration/confidence quality

### Operations

-   automation rate
-   escalation rate
-   exception rate
-   decision latency
-   action completion rate

### Safety

-   unauthorized-action rate
-   policy violation rate
-   incorrect automatic-action rate
-   maximum loss exposure
-   stopped unsafe workflows

### Baseline

Always compare against a deterministic/manual baseline.

Example:

``` text
Manual baseline
vs
AI decision system
```

Report both absolute and relative improvement.

------------------------------------------------------------------------

# 22. Baseline Principle

The baseline must be intentionally reasonable.

Do not create a deliberately stupid baseline just to make the AI look
good.

Examples:

-   fixed retry rule;
-   simple threshold-based anomaly detection;
-   moving-average demand forecast;
-   rule-based customer prioritization.

Then show whether the AI system actually improves over that baseline.

------------------------------------------------------------------------

# 23. Data Storage

Recommended prototype stack:

### PostgreSQL

Primary relational store for:

-   merchants
-   customers
-   orders
-   payments
-   refunds
-   decisions
-   actions
-   policies
-   outcomes
-   audit references

### Redis

For:

-   idempotency keys
-   short-lived state
-   rate limiting
-   caching
-   workflow locks

### Object storage

For:

-   model artifacts
-   datasets
-   evaluation reports
-   raw event archives where required

### Vector database

Only introduce if semantic retrieval genuinely becomes necessary.

Do not add a vector DB simply because the project contains an LLM.

------------------------------------------------------------------------

# 24. Backend

Recommended:

**Python + FastAPI**

Reasons:

-   strong ML ecosystem;
-   straightforward REST APIs;
-   Pydantic validation;
-   asynchronous support;
-   easy model integration;
-   fast prototype velocity.

Suggested service boundaries:

``` text
api/
ingestion/
context/
intelligence/
decision/
policy/
actions/
verification/
audit/
evaluation/
```

Start as a modular monolith.

Do NOT prematurely split into microservices.

------------------------------------------------------------------------

# 25. Frontend

Recommended:

**Next.js + TypeScript**

Primary screens:

1.  Merchant Overview
2.  Current Business State
3.  Decision Feed
4.  Decision Detail
5.  Action Center
6.  Approval Queue
7.  Audit Trail
8.  Evaluation / Impact
9.  Policy Configuration
10. Simulation / Replay

The UI should emphasize decisions and evidence, not charts for the sake
of charts.

------------------------------------------------------------------------

# 26. API Design

Recommended REST boundaries:

``` text
/api/v1/events
/api/v1/merchants
/api/v1/customers
/api/v1/orders
/api/v1/payments
/api/v1/context
/api/v1/decisions
/api/v1/recommendations
/api/v1/policies
/api/v1/actions
/api/v1/approvals
/api/v1/outcomes
/api/v1/audit
/api/v1/evaluation
/api/v1/simulation
```

Webhook endpoint:

``` text
POST /api/v1/webhooks/razorpay
```

Never expose Razorpay secrets to the frontend.

------------------------------------------------------------------------

# 27. Security Architecture

Security is a first-class subsystem.

Minimum requirements:

-   HTTPS/TLS
-   secrets stored outside source control
-   environment-specific credentials
-   least-privilege access
-   authentication
-   authorization/RBAC
-   rate limiting
-   input validation
-   webhook authenticity verification
-   replay/idempotency protection
-   secure logging
-   PII minimization
-   encryption at rest where applicable
-   dependency scanning
-   SAST
-   DAST
-   container scanning
-   security headers
-   CORS restrictions
-   CSRF protection where applicable
-   secure cookie configuration if cookies are used

------------------------------------------------------------------------

# 28. Web Application Security Assessment

The project should include a documented security assessment.

Scope:

### Authentication

-   invalid token
-   expired token
-   privilege escalation

### Authorization

-   merchant A accessing merchant B
-   user accessing another user's decisions
-   unauthorized action execution

### API

-   malformed requests
-   rate-limit bypass
-   excessive payloads
-   parameter tampering
-   mass assignment

### Webhooks

-   forged webhook
-   replayed webhook
-   duplicate webhook
-   reordered webhook
-   malicious payload

### AI

-   prompt injection
-   instruction hijacking
-   tool abuse
-   sensitive-data leakage
-   untrusted external context
-   model overreach

### Financial controls

-   action beyond monetary limit
-   retry-limit bypass
-   approval bypass
-   duplicate financial action

### OWASP alignment

Map findings to the relevant OWASP Web Security Testing / API Security
categories where applicable.

The security assessment must report:

``` text
Finding
Severity
Attack scenario
Impact
Evidence
Remediation
Retest result
```

------------------------------------------------------------------------

# 29. Observability

Every production-like workflow must be observable.

Recommended stack:

-   structured application logs
-   request IDs
-   correlation IDs
-   decision IDs
-   Prometheus metrics
-   Grafana dashboards
-   error tracking
-   audit log store

Important metrics:

``` text
events_received
events_rejected
webhook_duplicates
decisions_created
decisions_auto_executed
decisions_escalated
actions_failed
verification_failures
policy_blocks
model_latency
decision_latency
api_latency
```

------------------------------------------------------------------------

# 30. Logging Rules

Logs must answer:

> What happened?

> Where?

> When?

> For which merchant?

> Which decision?

> Which action?

> What failed?

Do not log:

-   API secrets
-   full authentication credentials
-   unnecessary payment-sensitive data
-   raw sensitive personal information

Use structured JSON logs.

------------------------------------------------------------------------

# 31. GitHub Repository Strategy

Recommended monorepo initially:

``` text
razorpay-decision-intelligence/
│
├── apps/
│   ├── web/
│   └── api/
│
├── packages/
│   ├── contracts/
│   ├── ui/
│   └── config/
│
├── intelligence/
│   ├── features/
│   ├── models/
│   ├── training/
│   └── evaluation/
│
├── workflows/
│
├── infra/
│
├── security/
│
├── datasets/
│   ├── synthetic/
│   └── evaluation/
│
├── docs/
│
├── scripts/
│
├── tests/
│
├── .github/
│   └── workflows/
│
├── docker/
│
├── .env.example
├── docker-compose.yml
├── README.md
└── LICENSE
```

Avoid multiple repositories unless deployment constraints make
separation necessary.

------------------------------------------------------------------------

# 32. Git Branch Strategy

``` text
main
  └── develop
       ├── feature/context-engine
       ├── feature/decision-engine
       ├── feature/policy-engine
       ├── feature/razorpay-integration
       ├── feature/evaluation
       ├── feature/security
       └── feature/web-ui
```

For a 9-day hackathon, keep branches short-lived.

Every feature branch:

``` text
feature/*
   ↓
Pull Request
   ↓
CI checks
   ↓
Review
   ↓
develop
   ↓
integration verification
   ↓
main
```

Hotfixes:

``` text
hotfix/*
```

------------------------------------------------------------------------

# 33. CI Pipeline

Every pull request should execute:

``` text
Checkout
  ↓
Dependency install
  ↓
Formatting
  ↓
Lint
  ↓
Type checking
  ↓
Unit tests
  ↓
Integration tests
  ↓
SAST
  ↓
Dependency vulnerability scan
  ↓
Build
```

Main branch additionally:

``` text
Container build
  ↓
Container vulnerability scan
  ↓
Deploy staging
  ↓
Smoke tests
  ↓
Evaluation sanity tests
```

Production deployment should require an explicit approval gate if we
actually deploy a production-like environment.

------------------------------------------------------------------------

# 34. CD Pipeline

``` text
Git Push
   ↓
CI
   ↓
Build
   ↓
Security Scans
   ↓
Artifact Registry
   ↓
Staging
   ↓
Integration Tests
   ↓
Approval
   ↓
Production
   ↓
Health Check
   ↓
Monitor
   ↓
Rollback if required
```

For the hackathon, "production" may be a controlled public demo
environment rather than a real-money deployment.

------------------------------------------------------------------------

# 35. Infrastructure

Do not over-engineer cloud infrastructure during the first
implementation.

Prototype target:

``` text
Next.js
    ↓
FastAPI
    ↓
PostgreSQL
    +
Redis
    +
Object Storage
```

Optional:

``` text
Message queue
Model serving
Observability stack
```

Kubernetes is not required unless the actual deployment needs it.

The architecture should remain cloud-portable.

------------------------------------------------------------------------

# 36. Reliability Contracts

## Idempotency

Every financial or consequential action must have an idempotency key.

## Retry

Retries must be:

-   bounded;
-   exponential where appropriate;
-   jittered;
-   policy-aware.

## Timeout

Every external call requires a timeout.

## Circuit breaking

Repeated downstream failures should stop automatic execution.

## Dead-letter handling

Events that cannot be processed must be retained for investigation
rather than silently discarded.

## Reconciliation

Internal state must periodically be reconciled against the authoritative
external state where applicable.

------------------------------------------------------------------------

# 37. AI Safety Contracts

The AI must never:

-   directly execute arbitrary financial operations;
-   invent transaction state;
-   invent external data;
-   bypass policy checks;
-   override approval requirements;
-   retry indefinitely;
-   claim success without verification;
-   expose secrets;
-   use untrusted text as executable instructions.

The AI must:

-   produce structured outputs;
-   expose confidence/uncertainty where appropriate;
-   cite supporting evidence;
-   respect action authority;
-   stop when policy says stop;
-   escalate when uncertain or outside authority.

------------------------------------------------------------------------

# 38. Core Decision Contract

Every decision should conform conceptually to:

``` json
{
  "decision_id": "dec_x",
  "merchant_id": "merchant_x",
  "trigger_event": "evt_x",
  "state_snapshot": {},
  "context": {},
  "expectation": {},
  "signals": [],
  "decision": {},
  "confidence": 0.0,
  "expected_impact": {},
  "policy_evaluation": {},
  "authority_level": "RECOMMEND",
  "action": {},
  "verification_plan": {},
  "explanation": {},
  "model_version": "model_x",
  "policy_version": "policy_x",
  "created_at": "timestamp"
}
```

This is a conceptual contract. Exact schemas will be finalized during
implementation.

------------------------------------------------------------------------

# 39. Demo Scenarios

The final demo should not attempt to show every theoretical capability.

Recommended core scenarios:

## Scenario A --- Recoverable payment failure

Show:

``` text
Payment failure
→ context analysis
→ customer/value signal
→ expected recoverability
→ recommendation
→ policy gate
→ bounded action
→ successful/failed verification
→ audit trail
```

## Scenario B --- Risky decision blocked

Show:

``` text
AI recommendation
→ policy detects excessive risk
→ automatic action blocked
→ human escalation
→ reason shown
→ audit recorded
```

This demonstrates that the AI is not uncontrolled.

## Scenario C --- Business-context decision

Example:

``` text
Regional/seasonal demand signal
+
inventory state
+
historical sales
→ expected stock pressure
→ recommendation
→ expected impact
→ merchant approval
→ outcome verification
```

The exact scenario must be selected based on what can be implemented and
evaluated reliably.

------------------------------------------------------------------------

# 40. Final Demonstration Narrative

The demo should tell one story rather than showing disconnected screens.

### Opening

> "A merchant does not think in payment APIs. They think in business
> consequences."

### Then show

1.  A real merchant state.
2.  A meaningful event.
3.  The system interpreting context.
4.  The AI reasoning.
5.  The recommendation.
6.  The policy boundary.
7.  The action.
8.  The verification.
9.  The audit trail.
10. The measurable outcome.

Then deliberately trigger a failure.

Show:

> "The AI failed / the external system failed / the state became
> uncertain."

Then show:

> **The system stopped safely.**

That may be one of the strongest moments of the entire demo.

------------------------------------------------------------------------

# 41. What We Are Explicitly NOT Building

Unless a future decision proves strong value, do not add:

-   generic chatbot;
-   generic RAG assistant;
-   unnecessary vector database;
-   unrestricted autonomous agent;
-   five disconnected agents;
-   generic analytics dashboard;
-   blockchain;
-   unnecessary microservices;
-   Kubernetes solely for presentation;
-   dozens of external APIs;
-   fake real-time intelligence;
-   fabricated Razorpay capabilities;
-   unbounded financial automation;
-   features that cannot be evaluated.

------------------------------------------------------------------------

# 42. Architecture Decision Records

Every major architectural deviation should be recorded in:

``` text
docs/adr/
```

Format:

``` text
ADR-001-title.md
ADR-002-title.md
...
```

Each ADR contains:

-   Context
-   Decision
-   Alternatives
-   Trade-offs
-   Consequences
-   Status

This prevents the project from drifting during rapid development.

------------------------------------------------------------------------

# 43. Current Architecture Freeze

### Frozen

-   contextual merchant decision intelligence as the thesis;
-   event → context → expectation → decision → policy → action →
    verification → audit loop;
-   Razorpay as the transactional event/integration backbone;
-   synthetic external context where native data is unavailable;
-   AI/ML must materially affect decisions;
-   policy engine independent from the LLM;
-   bounded actions;
-   human approval gates;
-   explicit stopping rules;
-   verification before claiming success;
-   complete audit trail;
-   held-out evaluation;
-   security assessment;
-   observability;
-   CI/CD;
-   modular monolith as the initial implementation strategy.

### Not yet frozen

-   exact ML models;
-   exact feature set;
-   exact external context sources;
-   exact database schema;
-   exact cloud provider;
-   exact queue technology;
-   final demo scenarios;
-   final frontend design;
-   final evaluation metrics;
-   final action authority for each workflow.

These should be frozen only after validating actual Razorpay API
capabilities and the 9-day implementation cost.

------------------------------------------------------------------------

# 44. Immediate Next Engineering Tasks

Do these in order.

## Phase 1 --- Razorpay Capability Verification

1.  Generate Test Mode API keys.
2.  Confirm accessible APIs.
3.  Create test orders.
4.  Create test payments.
5.  Trigger success/failure scenarios.
6.  Configure test webhooks.
7.  Capture actual webhook payloads.
8.  Verify event ordering/duplication behavior.
9.  Test payment/order state reconciliation.
10. Document the exact fields available.

## Phase 2 --- Domain Schema

Define:

``` text
Merchant
Customer
Product
Order
Payment
Refund
Event
ContextSnapshot
Expectation
Decision
Policy
Action
Approval
Outcome
AuditEntry
```

## Phase 3 --- Decision Scenario

Select the first complete scenario.

It must satisfy:

``` text
Real problem
+
AI signal
+
Decision
+
Policy
+
Action
+
Verification
+
Audit
+
Metric
```

## Phase 4 --- Vertical Slice

Build one scenario end-to-end before building breadth.

## Phase 5 --- Evaluation

Create the synthetic dataset and held-out test set.

## Phase 6 --- Security

Perform the web application security assessment against the working
vertical slice.

## Phase 7 --- Expansion

Only after the first slice works, add the second and third scenarios.

------------------------------------------------------------------------

# 45. Validation Gate Before Any Feature Is Added

Before accepting a feature, answer:

1.  What real merchant problem does it solve?
2.  Which event triggers it?
3.  What context does it require?
4.  What is the expected state?
5.  What does AI contribute?
6.  What decision does the system make?
7.  What policy constrains it?
8.  What action occurs?
9.  How is success verified?
10. What happens when it fails?
11. What gets logged?
12. How is value measured?
13. Can we test it on unseen data?
14. Does it make the product more defensible?
15. Can we implement it within the deadline?

If these cannot be answered, the feature is not ready.

------------------------------------------------------------------------

# 46. Engineering Principle

The project should optimize for:

> **Depth × Evidence × Reliability × Safety × Differentiation**

---not:

> **Number of features × Number of AI agents × Number of technologies**

The final product should feel smaller than the architecture diagram.

That is intentional.

The architecture contains the machinery required to demonstrate a
serious system. The product surface should expose only what is necessary
to make the intelligence obvious.

------------------------------------------------------------------------

# 47. Current One-Sentence Definition

> **A Razorpay-integrated contextual decision-intelligence system that
> turns merchant transaction events and business context into
> explainable, policy-bounded, verifiable actions, while measuring
> whether those decisions actually create value.**

This is the current source of truth.

Any future implementation, architecture change, prompt to another LLM,
feature proposal, or technical decision should be evaluated against this
definition.
