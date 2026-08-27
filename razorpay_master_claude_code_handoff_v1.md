# RAZORPAY CONTEXTUAL MERCHANT DECISION INTELLIGENCE
## Master Claude Code Handoff Context — v1.0

**Project:** Razorpay Hackathon — Open Track  
**Current phase:** Pre-implementation / Claude Code onboarding  
**Time constraint:** Approximately 9 days

---

## 1. HOW THIS WORKS

Claude Code is the implementation agent. The project owner will continue bringing architecture/product decisions, outputs, screenshots, errors, and proposals to an external validation layer.

Do not independently redefine the product.

For any requested change:
1. Understand the intent.
2. Inspect the repository/code first.
3. Identify architectural consequences.
4. Flag conflicts before implementing.
5. Prefer the smallest robust implementation.
6. Do not add technology or features merely for appearance.

Optimize for:

> **Depth × Evidence × Reliability × Safety × Differentiation**

not feature count or technology count.

---

## 2. PRODUCT THESIS

Razorpay already sits close to a merchant's transactional reality.

We are building a **contextual merchant decision-intelligence layer** that transforms transactional events plus merchant/business context into:

- understandable business state;
- expectations;
- AI-assisted decisions;
- policy-bounded actions;
- verified outcomes;
- audit trails;
- measurable evidence of value.

Core loop:

> **EVENT → CONTEXT → EXPECTATION → DECISION → POLICY GATE → ACTION → VERIFICATION → AUDIT → FEEDBACK**

The product is NOT:
- a generic chatbot;
- a generic analytics dashboard;
- five disconnected AI agents;
- a meta-agent coordinating official tracks;
- an LLM wrapper;
- unrestricted autonomous financial automation;
- technology bloat.

The merchant should experience a business decision system, not internal agents.

---

## 3. JETHALAL MENTAL MODEL

Use a small high-value electronics merchant as the concrete narrative model.

Jethalal:
- buys goods wholesale;
- sells to customers;
- handles high-value electronics;
- has first-time and repeat customers;
- depends on cash flow;
- wants reliable growth;
- maintains customer and supplier trust;
- protects reputation;
- needs operational control;
- operates under uncertainty.

This is a narrative anchor, not a hard-coded persona.

Generic model:

> **Merchant + Customers + Products + Orders + Payments + Inventory + Suppliers + External Context + Business Objectives**

---

## 4. FIVE MERCHANT OBJECTIVES

1. **Survival** — cash-flow stability and loss prevention.
2. **Trust** — reliable customer/supplier/payment interactions.
3. **Reputation** — consistent service and avoiding preventable failures.
4. **Control** — knowing what is actually happening and why.
5. **Growth** — conversion, recovered revenue, customer value, better decisions.

"Peace of mind" is an outcome of control, reliability and predictability rather than a separate optimization target.

The system must recognize trade-offs between these objectives.

---

## 5. NON-NEGOTIABLE HACKATHON BAR

The final product must demonstrate:

- a real problem;
- a working product;
- meaningful AI;
- measurable value;
- explainable consequential decisions;
- bounded actions;
- gated money actions;
- explicit stopping rules;
- compliant escalation;
- graceful failure;
- complete audit trail;
- held-out evaluation;
- reasonable baseline comparison;
- technical depth;
- reliability;
- security.

One cherry-picked match proves nothing.

A highly accurate model that can make unsafe decisions is not acceptable.

---

## 6. FUNDAMENTAL CONTROL BOUNDARY

> **THE AI RECOMMENDS.  
> THE POLICY ENGINE AUTHORIZES.  
> THE ACTION ORCHESTRATOR EXECUTES.  
> THE VERIFICATION ENGINE CONFIRMS.  
> THE AUDIT ENGINE RECORDS.**

The LLM must never bypass policy or independently authorize unrestricted financial actions.

Authority levels:

- **Level 0:** Observe
- **Level 1:** Recommend
- **Level 2:** Prepare / Approval Required
- **Level 3:** Bounded Automatic Action
- **Level 4:** Forbidden

Every automated workflow needs explicit stopping rules.

---

## 7. TARGET ARCHITECTURE

```text
Razorpay APIs + Webhooks
          ↓
Event Ingestion
(Auth + Validation + Deduplication)
          ↓
Canonical Event / State Store
          ↓
Context Engine
          ↓
Expectation + Feature Layer
          ↓
AI / ML Intelligence
          ↓
Decision Engine
          ↓
Policy + Constraint Engine
          ↓
 ┌────────┴─────────┐
 │                  │
Recommend/          Approval Gate
Escalate
 │                  │
 └────────┬─────────┘
          ↓
Action Orchestrator
(Idempotency + Retry + Timeout)
          ↓
Razorpay / Merchant Workflow
          ↓
Verification + Reconciliation
          ↓
Outcome + Feedback
          ↓
Context

Parallel:
Decision → Explanation → Audit Trail

All critical components → Logs / Metrics / Traces

Evaluation:
Synthetic Merchant Universe
→ Training/Calibration
→ Validation
→ Held-out Test Set
→ Baseline Comparison
→ Evidence
```

Initial architecture: **modular monolith**. Do not prematurely create microservices.

---

## 8. RAZORPAY DATA MODEL

### A. GENUINELY PROVIDED BY RAZORPAY

Examples:
- orders;
- payments;
- payment attempts;
- payment status;
- failure information;
- authorization/capture state;
- refunds;
- customer profile information;
- webhook events.

Follow actual verified responses. Never invent fields.

### B. DERIVED BY OUR SYSTEM

Examples:
- first-time vs repeat customer;
- historical spend;
- customer lifetime value;
- average order value;
- retry patterns;
- customer segments;
- recovery probability;
- expected behavior;
- anomaly scores;
- merchant baselines.

### C. PROJECT-OWNED / SYNTHETIC

Examples:
- inventory;
- products;
- margins;
- suppliers;
- supplier lead time;
- merchant objectives;
- regional demand;
- seasonality;
- weather;
- market conditions;
- evaluation ground truth.

Never present synthetic or derived information as native Razorpay data.

---

## 9. CRITICAL PAYMENT-ATTEMPT CORRECTION

A `payment.failed` payment cannot simply be captured later.

Capture applies to an already-authorized payment.

A later successful retry is a **NEW payment attempt associated with the same order**.

Correct model:

```text
ORDER
 ├── PAYMENT ATTEMPT 1 → failed
 ├── PAYMENT ATTEMPT 2 → failed
 └── PAYMENT ATTEMPT 3 → captured
```

Incorrect:

```text
payment.failed → same payment → payment.captured
```

The system must maintain:

- order-level aggregate state;
- payment-attempt-level state.

---

## 10. WEBHOOK PRINCIPLES

Webhooks are signals, not unquestionable final truth.

For consequential state:

```text
Webhook
→ signature validation
→ event ID deduplication
→ processing
→ API verification where required
→ reconciliation
→ internal state update
```

Important constraints:
- webhook ordering must not be assumed;
- duplicate delivery must be handled;
- `x-razorpay-event-id` is used for deduplication;
- critical state should be verified through API calls where appropriate;
- order-level and payment-level state must not be conflated.

Never expose/log API secrets or webhook secrets.

---

## 11. FIRST VERTICAL SLICE

Candidate: **recoverable payment failure / payment recovery intelligence**.

It must NOT be described as silently recharging a failed payment.

Conceptual flow:

```text
Payment failure
→ Context analysis
→ AI estimates recovery opportunity
→ Policy checks risk/authority/limits
→ Recommend or execute permitted bounded action
→ Customer retry / permitted payment workflow
→ New payment attempt
→ Verification
→ Outcome
→ Audit
```

Potential bounded actions:
- retry prompt;
- allowed payment workflow;
- recovery recommendation;
- capture of an already-authorized payment where applicable and permitted.

Alternative strong scenario:

```text
AI recommends action
→ policy detects excessive risk
→ action blocked
→ merchant escalation
→ reason shown
→ audit recorded
```

Final scenario depends on verified Razorpay capability, AI contribution, feasibility, measurable outcome and demo quality.

---

## 12. EXPECTATION ENGINE

The system needs a baseline for "what normally should happen."

Examples:
- expected payment success rate;
- expected recovery rate;
- expected order completion;
- expected customer behavior;
- expected product demand;
- expected inventory coverage.

Compare:

> **Observed state vs Expected state**

Do not treat every event as an anomaly.

---

## 13. AI / ML

Potential model families:
- XGBoost;
- LightGBM;
- Isolation Forest;
- Logistic Regression;
- calibrated classifiers;
- statistical baselines;
- forecasting models.

These are candidates, not blindly frozen.

For every model answer:
1. What decision does it influence?
2. What data does it require?
3. What is the ground truth?
4. What baseline are we comparing against?
5. What metric measures success?
6. What happens when it is wrong?
7. What policy protects the system?

LLMs may handle:
- natural-language merchant intent;
- structured-context reasoning;
- explanations;
- merchant-facing recommendations.

LLMs may NOT:
- bypass policy;
- authorize unrestricted money actions;
- invent state/evidence;
- claim success without verification;
- execute arbitrary tools based on untrusted instructions.

---

## 14. DECISION CONTRACT

Input:

```text
Merchant State
+ Current Event
+ Derived Context
+ Expectation
+ AI Signals
+ Business Objectives
+ Constraints
```

Output:

```text
Decision
+ Priority
+ Confidence
+ Expected Impact
+ Evidence
+ Authority Level
+ Action
+ Expiration
```

Conceptual structure:

```json
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

Exact implementation schema remains open until justified by the vertical slice.

---

## 15. POLICY ENGINE

Independent from the LLM.

Evaluate:
- monetary limits;
- retry limits;
- action frequency;
- merchant policy;
- risk thresholds;
- approval requirements;
- time restrictions;
- category restrictions;
- stopping rules;
- escalation.

Conceptual result:

```json
{
  "allowed": false,
  "authority_level": "RECOMMEND",
  "reason_codes": ["AMOUNT_EXCEEDS_AUTO_LIMIT"],
  "requires_approval": true,
  "policy_version": "policy_v1"
}
```

Policy decisions should be deterministic/reproducible where possible.

---

## 16. ACTION + VERIFICATION

Action orchestrator:
- executes approved action;
- idempotency;
- bounded retries;
- timeouts;
- state transitions;
- external API calls;
- result recording.

It does not make independent business decisions.

Verification:

```text
Action
→ API response
→ authoritative state check
→ actual business outcome
```

Never claim success merely because an API request succeeded.

---

## 17. AUDIT / EXPLAINABILITY

Every consequential decision should record, as applicable:

- event ID;
- decision ID;
- merchant ID;
- timestamp;
- state snapshot;
- context;
- expectation;
- model version;
- model score;
- decision;
- evidence;
- policy version;
- policy result;
- approval;
- action;
- API response reference;
- verification result;
- final outcome.

The UI should answer:

- Why this?
- Why now?
- Why this merchant/order/customer?
- Why this action?
- Why not another?
- What evidence supported it?
- What policy constrained it?
- What happened afterward?

---

## 18. GRACEFUL FAILURE

At least one failure must be deliberately demonstrated.

```text
AI detects opportunity
→ Policy allows bounded action
→ Action/external system fails or state becomes uncertain
→ Verification detects uncertainty
→ No blind infinite retry
→ Stopping rule
→ Safe escalation/fallback
→ Audit
```

Safe failure is a product capability.

---

## 19. EVALUATION

Build a synthetic merchant universe:

- 50+ merchants;
- multiple categories;
- multiple customer segments;
- hundreds/thousands of transactions;
- normal/abnormal behavior;
- multiple failure patterns;
- inventory conditions;
- seasonal conditions;
- injected edge cases;
- known ground truth.

Split:

```text
Training/Historical
→ Validation
→ UNTOUCHED HELD-OUT TEST
```

Do not tune on the held-out set.

Use a reasonable baseline such as:
- fixed retry rule;
- threshold anomaly detector;
- moving average;
- simple classifier;
- rule-based prioritization.

Measure, depending on scenario:

Decision:
- precision;
- recall;
- false-positive rate;
- false-negative rate;
- calibration.

Business:
- recovered revenue;
- prevented loss;
- conversion;
- unnecessary interventions.

Operations:
- automation rate;
- escalation rate;
- exception rate;
- action success;
- latency.

Safety:
- unauthorized-action rate;
- policy violations;
- unsafe automation;
- duplicate action;
- automated exposure;
- stopped unsafe workflows.

---

## 20. TECHNOLOGY DIRECTION

Preferred:

**Backend:** Python + FastAPI  
**Frontend:** Next.js + TypeScript  
**Database:** PostgreSQL  
**Short-lived state/cache/idempotency:** Redis  
**ML:** Python ecosystem  
**Architecture:** Modular monolith

Vector DB, Kafka, Kubernetes, agent frameworks, microservices, etc. require a concrete reason. Do not add them for appearance.

---

## 21. SECURITY

First-class requirements:

- HTTPS/TLS;
- secrets outside source control;
- least privilege;
- authentication;
- authorization/RBAC;
- tenant isolation;
- rate limiting;
- input validation;
- webhook signature validation;
- replay protection;
- idempotency;
- PII minimization;
- secure logs;
- dependency scanning;
- SAST;
- DAST;
- container scanning;
- security headers;
- restricted CORS;
- CSRF protection where applicable.

Test:
- authentication;
- authorization;
- tenant isolation;
- API abuse;
- webhook forgery/replay;
- prompt injection;
- tool abuse;
- data leakage;
- policy bypass;
- financial action abuse.

Report:

```text
Finding
Severity
Attack Scenario
Impact
Evidence
Remediation
Retest Result
```

---

## 22. OBSERVABILITY

Use structured logs and correlation IDs.

Important identifiers:
- request ID;
- correlation ID;
- merchant ID;
- decision ID;
- action ID;
- event ID.

Track:

```text
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

Never log secrets or unnecessary sensitive payment information.

---

## 23. GITHUB

Preferred monorepo:

```text
razorpay-decision-intelligence/
├── apps/
│   ├── web/
│   └── api/
├── packages/
│   ├── contracts/
│   ├── ui/
│   └── config/
├── intelligence/
│   ├── features/
│   ├── models/
│   ├── training/
│   └── evaluation/
├── workflows/
├── infra/
├── security/
├── datasets/
│   ├── synthetic/
│   └── evaluation/
├── docs/
├── scripts/
├── tests/
├── .github/workflows/
├── docker/
├── .env.example
├── docker-compose.yml
└── README.md
```

Branches:

```text
main
└── develop
    ├── feature/razorpay-integration
    ├── feature/event-ingestion
    ├── feature/context-engine
    ├── feature/intelligence
    ├── feature/decision-engine
    ├── feature/policy-engine
    ├── feature/evaluation
    ├── feature/security
    └── feature/web-ui
```

Use short-lived branches.

---

## 24. CI/CD

Eventually PR CI:

```text
Checkout
→ install
→ formatting
→ lint
→ type check
→ unit tests
→ integration tests
→ SAST
→ dependency scan
→ build
```

Main:

```text
container build
→ vulnerability scan
→ staging
→ smoke tests
→ evaluation sanity tests
```

Deployment:

```text
Git
→ CI
→ build
→ security
→ artifact
→ staging
→ integration
→ approval
→ demo/production
→ health check
→ monitoring
→ rollback
```

Do not overbuild deployment infrastructure before the application exists.

---

## 25. NON-GOALS / BLOAT FILTER

Do not add merely for appearance:

- generic chatbot;
- generic RAG;
- vector DB without real retrieval need;
- five disconnected agents;
- meta-agent;
- blockchain;
- Kubernetes without need;
- unnecessary Kafka;
- unnecessary microservices;
- dozens of external APIs;
- fake real-time data;
- fabricated Razorpay capabilities;
- unrestricted financial automation;
- excessive dashboarding.

Any new technology must justify itself through product value, evidence, reliability, safety or differentiation.

---

## 26. FEATURE VALIDATION GATE

Before implementing any new feature, answer:

1. What real merchant problem does it solve?
2. What event triggers it?
3. What context does it require?
4. What is the expected state?
5. What does AI contribute?
6. What decision is made?
7. What policy constrains it?
8. What action occurs?
9. How is success verified?
10. What happens when it fails?
11. What gets logged?
12. How is value measured?
13. Can it be tested on unseen data?
14. Does it strengthen the product?
15. Can it be implemented within nine days?

If these cannot be answered, do not build the feature yet.

---

## 27. ARCHITECTURE DECISION RECORDS

Major architectural changes go in:

```text
docs/adr/
```

Each ADR:

- Context;
- Decision;
- Alternatives;
- Trade-offs;
- Consequences;
- Status.

Do not silently change the architecture.

---

## 28. PHASE 1 HANDS-ON VERIFICATION STATUS

Claude Chat performed desk research but could not perform genuine hands-on Razorpay calls because its sandbox could not reach `api.razorpay.com` and had no Razorpay account.

Therefore actual evidence must come from the owner's local machine/Test Mode.

The prepared runbook covers:
- authentication;
- order creation;
- manual-capture order;
- fetch order;
- failed Checkout attempt;
- successful retry;
- multiple attempts;
- fetch payments for order;
- individual payment fetch;
- authorized-payment capture;
- webhook setup;
- payment.authorized/captured/failed;
- order.paid;
- signature validation;
- event ID;
- duplicate delivery;
- ordering;
- observed payment method values.

Unverified behavior must remain explicitly marked **UNVERIFIED**.

Never fabricate observations.

---

## 29. PROVIDED VERIFICATION FILES

### `phase1_hands_on_verification_runbook.md`

Contains the complete local 13-step verification process.

### `phase1_checkout_test.html`

Standalone browser harness that:
- loads Razorpay Checkout;
- accepts Test Mode Key ID;
- accepts Order ID;
- accepts amount;
- shows success callback JSON;
- shows payment failure callback JSON.

It must never contain the Key Secret.

### `verify_webhook_signature.py`

Local helper that:
- takes webhook secret locally;
- takes exact raw webhook body locally;
- computes HMAC-SHA256;
- lets the owner compare it with `X-Razorpay-Signature`.

Only report `MATCH` or `MISMATCH` back to the reasoning layer.

---

## 30. NINE-DAY ORDER OF WORK

1. Repository onboarding.
2. Razorpay integration verification.
3. Canonical event/state model.
4. Domain schema.
5. First complete vertical slice.
6. AI/ML integration.
7. Policy and safety.
8. Verification/audit.
9. Synthetic evaluation + held-out test.
10. UI.
11. Security assessment.
12. Observability + CI/CD.
13. Hardening and final demo.
14. Expand only if the first slice is solid.

Depth > breadth.

---

## 31. CURRENTLY FROZEN

- contextual merchant decision-intelligence thesis;
- Jethalal/generic merchant model;
- five merchant objectives;
- complete decision loop;
- AI/policy/action separation;
- bounded authority levels;
- stopping rules;
- verification requirement;
- audit requirement;
- held-out evaluation;
- reasonable baseline;
- security assessment;
- observability;
- modular-monolith starting architecture;
- Razorpay as transactional backbone;
- Razorpay-vs-derived-vs-synthetic data distinction;
- payment-attempt vs order-level distinction.

---

## 32. NOT YET FROZEN

- exact ML model;
- exact features;
- exact final demo scenario;
- exact external context;
- exact implementation schema;
- cloud provider;
- queue;
- model serving;
- final UI design;
- exact evaluation metrics;
- authority level for every future action.

These must be decided based on evidence and implementation needs.

---

# 33. CLAUDE CODE FIRST TASK — ONBOARDING ONLY

Before writing/modifying/deleting/installing anything:

1. Read this document completely.
2. Read the attached Razorpay capability map.
3. Read the architecture contract if separately attached.
4. Inspect the entire repository.
5. Inspect Git state.
6. Identify existing files, dependencies, configuration, tests and infrastructure.
7. Determine whether the repository is empty, partially initialized, already implemented, or contains unrelated work.
8. Compare repository reality against this document.
9. Identify contradictions and unresolved assumptions.
10. Do not silently resolve contradictions.
11. Do not begin feature implementation.
12. Do not install large dependencies merely because they may eventually be useful.
13. Do not make destructive changes.

Produce a **PROJECT ONBOARDING REPORT** containing:

A. Product thesis in your own words.
B. Merchant problem.
C. Complete decision loop.
D. Five objectives.
E. Razorpay-provided data.
F. Derived data.
G. Synthetic/project-owned data.
H. Razorpay constraints.
I. Payment-attempt vs order-level model.
J. AI/policy/action boundary.
K. Evaluation bar.
L. Security bar.
M. Current repository state.
N. Existing dependencies.
O. Git state.
P. Architecture conflicts.
Q. Unresolved assumptions.
R. Recommended nine-day implementation sequence.
S. Exact first implementation task.

**DO NOT BUILD YET.**

Wait for review/approval before major implementation.

---

# 34. FINAL PRODUCT STANDARD

The final demo should make this visible:

```text
Real merchant problem
        ↓
Real transactional event
        ↓
Contextual understanding
        ↓
Meaningful AI
        ↓
Concrete decision
        ↓
Policy boundary
        ↓
Bounded action / approval
        ↓
Verification
        ↓
Audit trail
        ↓
Measured outcome
```

Then demonstrate failure:

```text
Failure / uncertainty
        ↓
Detection
        ↓
Stopping rule
        ↓
Safe escalation
        ↓
Audit
```

The desired impression is not:

> "They used many AI technologies."

It is:

> **"They built a serious decision system around payment reality and thought about what happens when the AI is wrong."**

---

# 35. ONE-SENTENCE DEFINITION

> **A Razorpay-integrated contextual decision-intelligence system that turns merchant transaction events and business context into explainable, policy-bounded, verifiable actions, while measuring whether those decisions actually create value.**

This document is the current project source of truth.
