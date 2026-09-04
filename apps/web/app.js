"use strict";

/**
 * Payment Recovery Lab -- vanilla JS, no build step, no framework. Talks
 * only to the existing HTTP API via relative fetch() calls (same origin,
 * no hardcoded host/port, no CORS needed).
 *
 * Renders exactly what the API returns. A field the API sends as null is
 * rendered as an explicit "Not reached" / "Not available" state -- never
 * as a fabricated success. Status-to-color mapping below mirrors the
 * semantic grouping already established by the backend's own
 * ActionStatus/verification-result vocabulary; it does not invent new
 * states.
 */

// ---------------------------------------------------------------------------
// Centralized API access
// ---------------------------------------------------------------------------

const Api = {
  async _request(path, options) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (networkErr) {
      throw new Error("Network error -- could not reach the API.");
    }
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      // no/invalid JSON body -- fall through, handled below
    }
    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `Request failed (HTTP ${response.status}).`;
      throw new Error(detail);
    }
    return body;
  },

  health() {
    return this._request("/health");
  },
  listMerchants() {
    return this._request("/merchants");
  },
  merchantPayments(merchantId) {
    return this._request(`/merchants/${encodeURIComponent(merchantId)}/payments`);
  },
  merchantMetrics(merchantId) {
    return this._request(`/merchants/${encodeURIComponent(merchantId)}/metrics`);
  },
  orderTimeline(orderId) {
    return this._request(`/orders/${encodeURIComponent(orderId)}/timeline`);
  },
  reconcile(merchantId, orderId) {
    return this._request(
      `/merchants/${encodeURIComponent(merchantId)}/orders/${encodeURIComponent(orderId)}/reconcile`,
      { method: "POST" },
    );
  },
  simulate(payload) {
    return this._request("/decision-lab/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
};

// ---------------------------------------------------------------------------
// Status semantics -- one place, reused everywhere a status badge appears
// ---------------------------------------------------------------------------

const STATUS_SEMANTICS = {
  VERIFIED_SUCCESS: "success",
  VERIFIED_FAILED: "failed",
  ESCALATED: "warning",
  APPROVAL_PENDING: "warning",
  VERIFYING: "warning",
  VERIFICATION_UNCERTAIN: "warning",
  BLOCKED: "blocked",
  AUTHORIZED: "neutral",
  EXECUTING: "neutral",
  NO_ACTION: "neutral",
};

function statusClass(status) {
  return STATUS_SEMANTICS[status] || "neutral";
}

// payment_attempts.status is a SEPARATE, smaller vocabulary
// (created/authorized/captured/failed/refunded) from ActionStatus --
// deliberately not reused from STATUS_SEMANTICS so a payment-attempt
// status is never visually conflated with an action/verification
// outcome.
const PAYMENT_ATTEMPT_STATUS_SEMANTICS = {
  captured: "success",
  authorized: "neutral",
  failed: "failed",
  refunded: "neutral",
  created: "pending",
};

function paymentAttemptStatusBadge(status) {
  if (!status) return badge("—", "pending");
  const semanticClass = PAYMENT_ATTEMPT_STATUS_SEMANTICS[status] || "neutral";
  return badge(status, semanticClass);
}

function badge(text, semanticClass) {
  const span = document.createElement("span");
  span.className = `badge badge-${semanticClass}`;
  span.textContent = text;
  return span;
}

function statusBadge(status) {
  if (!status) return badge("—", "pending");
  return badge(status, statusClass(status));
}

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function show(node) { node.classList.remove("hidden"); }
function hide(node) { node.classList.add("hidden"); }

function formatAmount(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  return `${(amount / 100).toFixed(2)} ${currency || ""}`.trim();
}

function formatRupees(amount) {
  if (amount === null || amount === undefined) return "—";
  return `₹${(amount / 100).toFixed(2)}`;
}

function kvRow(key, valueNode) {
  const row = el("div", "kv-row");
  row.appendChild(el("span", "kv-key", key));
  const valWrap = el("span", "kv-val");
  if (typeof valueNode === "string" || typeof valueNode === "number") {
    valWrap.textContent = String(valueNode);
  } else if (valueNode instanceof Node) {
    valWrap.appendChild(valueNode);
  } else {
    valWrap.textContent = "—";
  }
  row.appendChild(valWrap);
  return row;
}

function qs(sel, root) { return (root || document).querySelector(sel); }
function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ---------------------------------------------------------------------------
// Application state
// ---------------------------------------------------------------------------

const state = {
  merchantId: null,
  merchantName: null,
};

// The one real order this page's hero/recovery/proof sections showcase --
// a real, previously-verified Razorpay Test Mode order. This is just an
// id reference (not business logic): every section reads exactly whatever
// GET /orders/{id}/timeline (and, for "Run the recovery",
// POST .../reconcile) returns right now, live, with no hardcoded outcome.
const SHOWCASE_ORDER_ID = "order_TXueleNMbhnp2s";

// ---------------------------------------------------------------------------
// Health indicator
// ---------------------------------------------------------------------------

async function refreshHealth() {
  const indicator = document.getElementById("health-indicator");
  const text = indicator.querySelector(".health-text");
  try {
    await Api.health();
    indicator.classList.remove("down");
    indicator.classList.add("ok");
    text.textContent = "API healthy";
  } catch (err) {
    indicator.classList.remove("ok");
    indicator.classList.add("down");
    text.textContent = "API unreachable";
  }
}

// ---------------------------------------------------------------------------
// Dashboard (console)
// ---------------------------------------------------------------------------

async function renderDashboard() {
  const loading = document.getElementById("dashboard-loading");
  const errorBox = document.getElementById("dashboard-error");
  const emptyBox = document.getElementById("dashboard-empty");
  const content = document.getElementById("dashboard-content");

  hide(errorBox);
  hide(emptyBox);
  hide(content);
  show(loading);

  let merchants;
  try {
    merchants = await Api.listMerchants();
  } catch (err) {
    hide(loading);
    errorBox.textContent = `Could not load merchants: ${err.message}`;
    show(errorBox);
    return;
  }

  if (!merchants.merchants || merchants.merchants.length === 0) {
    hide(loading);
    emptyBox.innerHTML =
      "No merchant exists in this database yet. This console reads live data only -- " +
      "seed exactly one demo merchant (with a real <code>policy_config</code>) before " +
      "running the demo. See the deployment notes for the one-time setup snippet.";
    show(emptyBox);
    return;
  }

  const merchant = merchants.merchants[0];
  state.merchantId = merchant.id;
  state.merchantName = merchant.name;
  document.getElementById("merchant-name").textContent = merchant.name;

  let payments, metrics;
  try {
    [payments, metrics] = await Promise.all([
      Api.merchantPayments(merchant.id),
      Api.merchantMetrics(merchant.id),
    ]);
  } catch (err) {
    hide(loading);
    errorBox.textContent = `Could not load dashboard data: ${err.message}`;
    show(errorBox);
    return;
  }

  renderMetricsStrip(metrics);
  renderOrdersTable(payments.orders);

  hide(loading);
  show(content);
}

function renderMetricsStrip(metrics) {
  const strip = document.getElementById("metrics-strip");
  clear(strip);

  strip.appendChild(
    metricTile(
      "Decisions",
      Object.values(metrics.decision_type_distribution.counts).reduce((a, b) => a + b, 0),
      "by decision_type",
      Object.entries(metrics.decision_type_distribution.counts).map(([k, v]) => `${k}: ${v}`),
      "What the engine recommended -- not a claim that any recommendation was correct.",
    ),
  );

  const policy = metrics.policy_outcome_distribution;
  strip.appendChild(
    metricTile(
      "Policy outcomes",
      policy.allow + policy.approval_required + policy.block,
      "money-moving proposals",
      [`ALLOW: ${policy.allow}`, `APPROVAL_REQUIRED: ${policy.approval_required}`, `BLOCK: ${policy.block}`],
      "How Policy treated capture proposals -- independent of whether the underlying decision was right.",
    ),
  );

  const capture = metrics.capture_terminal_status_distribution;
  strip.appendChild(
    metricTile(
      "Capture outcomes",
      capture.verified_success + capture.verified_failed + capture.escalated + capture.blocked,
      "terminal capture actions",
      [
        `VERIFIED_SUCCESS: ${capture.verified_success}`,
        `VERIFIED_FAILED: ${capture.verified_failed}`,
        `ESCALATED: ${capture.escalated}`,
        `BLOCKED: ${capture.blocked}`,
      ],
      "BLOCKED means Policy prevented execution -- it is not a verification failure.",
    ),
  );

  const escalation = metrics.escalation_metrics;
  strip.appendChild(
    metricTile(
      "Escalations",
      escalation.total_escalated,
      "capture actions escalated",
      Object.entries(escalation.by_reason).map(([k, v]) => `${k}: ${v}`),
      "Verification could not determine a definitive outcome for these.",
    ),
  );

  const amount = metrics.verified_captured_amount;
  strip.appendChild(
    metricTile(
      "Verified captured amount",
      formatAmount(amount.total_verified_captured_amount, ""),
      `${amount.verified_success_count} verified capture(s)`,
      [],
      "Amount independently re-confirmed via a live Razorpay read. Not a claim about how well the engine performs or a revenue-impact figure.",
    ),
  );

  const timing = metrics.verification_resolution_timing;
  strip.appendChild(
    metricTile(
      "Resolution time",
      timing.count > 0 ? `${timing.avg_seconds.toFixed(1)}s avg` : "—",
      timing.count > 0 ? `min ${timing.min_seconds.toFixed(1)}s / max ${timing.max_seconds.toFixed(1)}s` : "no verified captures yet",
      [],
      "Wall-clock time from action proposal to verified success. Not a production SLA claim.",
    ),
  );

  const retry = metrics.retry_prompt_outcome_availability;
  strip.appendChild(
    metricTile(
      "Retry-prompt outcomes",
      retry.outcome_measurable ? "measurable" : "not measurable",
      `${retry.total_customer_retry_prompt_actions} retry-prompt action(s) recorded`,
      [],
      retry.reason,
    ),
  );
}

function metricTile(label, value, sub, breakdownLines, caveat) {
  const tile = el("div", "metric-tile");
  tile.appendChild(el("div", "metric-label", label));
  tile.appendChild(el("div", "metric-value", String(value)));
  if (sub) tile.appendChild(el("div", "metric-sub", sub));
  if (breakdownLines && breakdownLines.length) {
    const breakdown = el("div", "metric-breakdown");
    breakdownLines.forEach((line) => breakdown.appendChild(el("span", "reason-chip", line)));
    tile.appendChild(breakdown);
  }
  if (caveat) tile.appendChild(el("div", "metric-caveat", caveat));
  return tile;
}

function renderOrdersTable(orders) {
  const wrap = document.getElementById("orders-table-wrap");
  clear(wrap);

  if (!orders || orders.length === 0) {
    wrap.appendChild(el("div", "empty-state", "No orders recorded for this merchant yet."));
    return;
  }

  const table = el("table", "orders-table");
  const thead = el("thead");
  const headRow = el("tr");
  ["Order", "Amount", "Order Status", "Payment Attempt", "Payment Status", ""].forEach((h) => {
    headRow.appendChild(el("th", null, h));
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  orders.forEach(({ order, payment_attempts }) => {
    const row = el("tr");
    row.appendChild(el("td", "mono", order.id));
    row.appendChild(el("td", "mono", formatAmount(order.amount, order.currency)));
    const orderStatusCell = el("td");
    orderStatusCell.appendChild(badge(order.status, "neutral"));
    row.appendChild(orderStatusCell);

    const latestAttempt = payment_attempts.length ? payment_attempts[payment_attempts.length - 1] : null;
    row.appendChild(el("td", "mono", latestAttempt ? latestAttempt.id : "—"));

    const attemptStatusCell = el("td");
    attemptStatusCell.appendChild(paymentAttemptStatusBadge(latestAttempt ? latestAttempt.status : null));
    row.appendChild(attemptStatusCell);

    row.appendChild(el("td", null, "View →"));

    row.addEventListener("click", () => navigateToOrder(order.id));
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

// ---------------------------------------------------------------------------
// Order detail (console)
// ---------------------------------------------------------------------------

let reconcileInFlight = false;

async function renderOrderDetail(orderId) {
  const loading = document.getElementById("detail-loading");
  const errorBox = document.getElementById("detail-error");
  const content = document.getElementById("detail-content");
  const banner = document.getElementById("reconcile-banner");

  hide(errorBox);
  hide(content);
  hide(banner);
  show(loading);

  let timeline;
  try {
    timeline = await Api.orderTimeline(orderId);
  } catch (err) {
    hide(loading);
    errorBox.textContent = `Could not load order: ${err.message}`;
    show(errorBox);
    return;
  }

  hide(loading);
  show(content);
  paintOrderDetail(timeline);

  const btn = document.getElementById("reconcile-btn");
  btn.onclick = () => handleReconcile(orderId);
}

function paintOrderDetail(timeline) {
  paintOrderInfo(timeline);
  paintTracker(timeline);
  paintDecisionCard(timeline.decision);
  paintPolicyCard(timeline.policy);
  paintActionCard(timeline.action);
  paintVerificationCard(timeline.verification, timeline.outcome);
  paintAuditTimeline(timeline.audit);
}

function paintOrderInfo(timeline) {
  const wrap = document.getElementById("order-info");
  clear(wrap);
  wrap.appendChild(el("h2", null, timeline.order.id));

  const meta = el("div", "order-meta");
  const item = (label, value) => {
    const span = document.createElement("span");
    span.append(`${label}: `);
    const strong = el("strong", null, value);
    span.appendChild(strong);
    return span;
  };
  meta.appendChild(item("Amount", formatAmount(timeline.order.amount, timeline.order.currency)));
  meta.appendChild(item("Order status", timeline.order.status));
  meta.appendChild(item("Attempts", String(timeline.payment_attempts.length)));
  wrap.appendChild(meta);
}

function trackerStage(name, valueText, semanticClass) {
  const stage = el("div", `tracker-stage stage-${semanticClass}`);
  stage.appendChild(el("div", "stage-name", name));
  stage.appendChild(el("div", "stage-value", valueText));
  return stage;
}

function paintTracker(timeline) {
  const tracker = document.getElementById("pipeline-tracker");
  clear(tracker);

  const hasAttempts = timeline.payment_attempts.length > 0;
  tracker.appendChild(
    trackerStage("Reconciliation", hasAttempts ? "Observed" : "No data yet", hasAttempts ? "success" : "pending"),
  );

  const decision = timeline.decision;
  tracker.appendChild(
    trackerStage("Decision", decision ? decision.decision_type : "Not reached", decision ? "neutral" : "pending"),
  );

  const policy = timeline.policy;
  if (!policy) {
    tracker.appendChild(trackerStage("Policy", "Not reached", "pending"));
  } else if (policy.allowed === false) {
    tracker.appendChild(trackerStage("Policy", "BLOCK", "blocked"));
  } else if (policy.requires_approval) {
    tracker.appendChild(trackerStage("Policy", "APPROVAL_REQUIRED", "warning"));
  } else {
    tracker.appendChild(trackerStage("Policy", "ALLOW", "success"));
  }

  const action = timeline.action;
  tracker.appendChild(
    trackerStage("Action", action ? action.status : "Not reached", action ? statusClass(action.status) : "pending"),
  );

  const verification = timeline.verification;
  tracker.appendChild(
    trackerStage(
      "Verification",
      verification ? verification.result : "Not reached",
      verification ? statusClass(verification.result) : "pending",
    ),
  );

  const outcome = timeline.outcome;
  tracker.appendChild(
    trackerStage("Outcome", outcome ? formatAmount(outcome.recovered_amount, "") + " recovered" : "Not available", outcome ? "success" : "pending"),
  );
}

function cardHeading(card, title) {
  clear(card);
  card.appendChild(el("h3", null, title));
}

function reasonCodeChips(codes) {
  const wrap = el("div", "reason-codes");
  (codes || []).forEach((code) => wrap.appendChild(el("span", "reason-chip", code)));
  return wrap;
}

function paintDecisionCard(decision) {
  const card = document.getElementById("decision-card");
  cardHeading(card, "Decision");
  if (!decision) {
    card.appendChild(el("div", "card-empty", "No decision recorded for this order yet."));
    return;
  }
  card.appendChild(kvRow("Type", decision.decision_type));
  card.appendChild(kvRow("Confidence", decision.confidence.toFixed(3)));
  card.appendChild(kvRow("Model", decision.model_version));
  if (decision.expected_impact && decision.expected_impact.revenue_at_stake !== undefined) {
    card.appendChild(kvRow("Revenue at stake", formatAmount(decision.expected_impact.revenue_at_stake, "")));
  }
  card.appendChild(reasonCodeChips(decision.reason_codes));
  card.appendChild(
    el(
      "div",
      "card-note",
      "RuleBasedEngine is a deterministic rule engine -- it applies fixed rules, it was not fit to historical data. Confidence reflects certainty in the observed rule condition, not a statistically calibrated success probability.",
    ),
  );
}

function paintPolicyCard(policy) {
  const card = document.getElementById("policy-card");
  cardHeading(card, "Policy");
  if (!policy) {
    card.appendChild(el("div", "card-empty", "Not policy-gated (no action was proposed for this decision)."));
    return;
  }
  card.appendChild(kvRow("Allowed", policy.allowed === true ? "Yes" : policy.allowed === false ? "No" : "—"));
  card.appendChild(kvRow("Authority level", policy.authority_level_granted || "—"));
  card.appendChild(kvRow("Requires approval", policy.requires_approval ? "Yes" : "No"));
  card.appendChild(reasonCodeChips(policy.reason_codes));
}

function paintActionCard(action) {
  const card = document.getElementById("action-card");
  cardHeading(card, "Action");
  if (!action) {
    card.appendChild(el("div", "card-empty", "No action was proposed for this decision."));
    return;
  }
  card.appendChild(kvRow("Type", action.action_type));
  card.appendChild(kvRow("Status", statusBadge(action.status)));
  if (action.execution_reference && action.execution_reference.outcome) {
    card.appendChild(kvRow("Execution outcome", action.execution_reference.outcome));
  }
}

function paintVerificationCard(verification, outcome) {
  const card = document.getElementById("verification-card");
  cardHeading(card, "Verification & Outcome");
  if (!verification) {
    card.appendChild(el("div", "card-empty", "Not reached -- this action has not been verified."));
    return;
  }
  card.appendChild(kvRow("Result", statusBadge(verification.result)));
  if (verification.reason) card.appendChild(kvRow("Reason", verification.reason));
  if (verification.attempt_count !== undefined) card.appendChild(kvRow("Read attempts", verification.attempt_count));

  if (outcome) {
    card.appendChild(kvRow("Recovered amount", formatAmount(outcome.recovered_amount, "")));
    if (outcome.time_to_resolution_seconds !== undefined) {
      card.appendChild(kvRow("Time to resolution", `${outcome.time_to_resolution_seconds.toFixed(1)}s`));
    }
  } else {
    card.appendChild(el("div", "card-empty", "No recovered-amount outcome recorded (not a successful capture)."));
  }
}

function paintAuditTimeline(audit) {
  const wrap = document.getElementById("audit-timeline");
  clear(wrap);
  if (!audit || audit.length === 0) {
    wrap.appendChild(el("div", "card-empty", "No audit checkpoints recorded yet."));
    return;
  }
  audit.forEach((entry) => {
    const item = el("div", "audit-item");
    const header = el("div");
    header.appendChild(el("span", "audit-checkpoint", entry.checkpoint));
    header.appendChild(el("span", "audit-seq", `#${entry.sequence_number}`));
    item.appendChild(header);
    if (entry.snapshot && Object.keys(entry.snapshot).length > 0) {
      item.appendChild(el("div", "audit-snapshot", JSON.stringify(entry.snapshot)));
    }
    wrap.appendChild(item);
  });
}

// ---------------------------------------------------------------------------
// Reconcile interaction (console)
// ---------------------------------------------------------------------------

async function handleReconcile(orderId) {
  if (reconcileInFlight) return; // guard against duplicate/rendering-driven re-invocation
  reconcileInFlight = true;

  const btn = document.getElementById("reconcile-btn");
  const statusText = document.getElementById("reconcile-status");
  const banner = document.getElementById("reconcile-banner");

  btn.disabled = true;
  statusText.textContent = "Reconciling…";
  hide(banner);

  try {
    const result = await Api.reconcile(state.merchantId, orderId);
    banner.className = "banner banner-success";
    banner.textContent =
      result.new_event_count === 0
        ? "Reconciliation complete -- no new events discovered."
        : `Reconciliation complete -- ${result.new_event_count} new event(s) discovered.`;
    show(banner);

    const timeline = await Api.orderTimeline(orderId);
    paintOrderDetail(timeline);
  } catch (err) {
    banner.className = "banner banner-error";
    banner.textContent = `Reconciliation failed: ${err.message}`;
    show(banner);
  } finally {
    btn.disabled = false;
    statusText.textContent = "";
    reconcileInFlight = false;
  }
}

// ---------------------------------------------------------------------------
// Shared pipeline-stage card renderer -- used by BOTH "Run the recovery"
// (real API data) and "Try a scenario" (simulated decision/policy only).
// Keeping one renderer means the two experiences can never visually drift
// apart, and neither can silently start rendering fabricated content --
// every caller passes real field values or an explicit "not
// executed"/"not reached" string, never a guess.
// ---------------------------------------------------------------------------

function buildStageCard(stage) {
  const card = el("div", `pstage pstage-${stage.semantic || "pending"}${stage.simulated ? " pstage-simulated" : ""}`);

  const head = el("div", "pstage-head");
  head.appendChild(el("span", `pstage-badge pstage-badge-${stage.badgeClass || "engine"}`, stage.badge));
  head.appendChild(el("span", "pstage-title", stage.title));
  card.appendChild(head);

  const body = el("div", "pstage-body");
  (stage.lines || []).forEach((line) => body.appendChild(el("p", "pstage-line", line)));
  card.appendChild(body);

  if (stage.detailRows && stage.detailRows.length) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "detail-toggle";
    toggle.textContent = "Technical detail ▸";
    const panel = el("div", "detail-panel hidden");
    stage.detailRows.forEach(([k, v]) => {
      const row = el("div", "detail-kv");
      row.appendChild(el("span", "detail-kv-key", k));
      row.appendChild(el("span", "detail-kv-val", v === null || v === undefined || v === "" ? "—" : String(v)));
      panel.appendChild(row);
    });
    toggle.addEventListener("click", () => {
      const nowHidden = panel.classList.toggle("hidden");
      toggle.textContent = `Technical detail ${nowHidden ? "▸" : "▾"}`;
    });
    card.appendChild(toggle);
    card.appendChild(panel);
  }

  return card;
}

async function revealPipelineStages(container, stages) {
  clear(container);
  const delay = prefersReducedMotion() ? 0 : 220;
  for (const stage of stages) {
    const card = buildStageCard(stage);
    card.classList.add("pstage-enter");
    container.appendChild(card);
    // A plain setTimeout (not requestAnimationFrame) so this reveal
    // sequence still completes when the tab is backgrounded/hidden --
    // browsers can suspend rAF callbacks indefinitely in that case,
    // which would otherwise stall this loop on the very first card.
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 20));
    card.classList.add("pstage-enter-active");
    if (delay) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
}

function policyOutcomeText(policy) {
  if (policy.allowed === false) return "BLOCK";
  if (policy.requires_approval) return "APPROVAL_REQUIRED";
  return "ALLOW";
}

function policySemantic(policy) {
  if (policy.allowed === false) return "blocked";
  if (policy.requires_approval) return "warning";
  return "success";
}

// ---------------------------------------------------------------------------
// "Run the recovery" -- calls the REAL live API: fetches the order
// timeline, calls the real reconcile endpoint against Razorpay, re-fetches
// the timeline, then renders the actual returned decision/policy/action/
// verification/outcome. Never a hard-coded sequence of setTimeout calls.
// ---------------------------------------------------------------------------

let showcaseFetchPromise = null;

function fetchShowcaseTimeline() {
  if (!showcaseFetchPromise) {
    showcaseFetchPromise = Api.orderTimeline(SHOWCASE_ORDER_ID);
  }
  return showcaseFetchPromise;
}

function buildLiveStages(timeline) {
  const order = timeline.order;
  const latestAttempt = timeline.payment_attempts.length
    ? timeline.payment_attempts[timeline.payment_attempts.length - 1]
    : null;
  const decision = timeline.decision;
  const policy = timeline.policy;
  const action = timeline.action;
  const verification = timeline.verification;
  const outcome = timeline.outcome;
  const amountText = formatAmount(order.amount, order.currency);

  const stages = [];

  stages.push({
    badge: "RAZORPAY",
    badgeClass: "razorpay",
    title: "01 · Payment",
    semantic: latestAttempt ? paymentAttemptSemantic(latestAttempt) : "pending",
    lines: latestAttempt
      ? [`${amountText} — ${latestAttempt.status.toUpperCase()}`, latestAttempt.captured ? "captured = true" : "captured = false"]
      : ["No payment attempt observed for this order yet."],
    detailRows: [
      ["order id", order.id],
      ["payment id", latestAttempt ? latestAttempt.id : "—"],
      ["status", latestAttempt ? latestAttempt.status : "—"],
      ["captured", latestAttempt ? latestAttempt.captured : "—"],
    ],
  });

  stages.push({
    badge: "DECISION INTELLIGENCE",
    badgeClass: "engine",
    title: "02 · Decision",
    semantic: decision ? "neutral" : "pending",
    lines: [decision ? decision.decision_type : "Not reached", decision ? decisionPlainEnglish(decision.decision_type) : "No decision recorded for this order yet."],
    detailRows: decision
      ? [["model", decision.model_version], ["confidence", decision.confidence.toFixed(3)], ["reason codes", decision.reason_codes.join(", ")]]
      : [],
  });

  stages.push({
    badge: "MERCHANT POLICY",
    badgeClass: "policy",
    title: "03 · Policy",
    semantic: policy ? policySemantic(policy) : "pending",
    lines: policy
      ? [policyOutcomeText(policy), policyPlainEnglish(policy)]
      : ["Not policy-gated (no action was proposed for this decision)."],
    detailRows: policy
      ? [
          ["authority_level_granted", policy.authority_level_granted],
          ["requires_approval", policy.requires_approval],
          ["allowed", policy.allowed],
          ["reason codes", policy.reason_codes.join(", ")],
        ]
      : [],
  });

  const actOutcome = action && action.execution_reference ? action.execution_reference.outcome : null;
  stages.push({
    badge: "DECISION INTELLIGENCE → RAZORPAY",
    badgeClass: "engine",
    title: "04 · Action",
    semantic: action ? statusClass(action.status) : "pending",
    lines: action
      ? [`${action.action_type} — ${actOutcome ? actOutcome.toUpperCase() : action.status}`, "Sent through Razorpay's capture write path."]
      : ["No action was proposed for this decision -- nothing was sent to Razorpay."],
    detailRows: action ? [["action_type", action.action_type], ["status", action.status], ["execution outcome", actOutcome]] : [],
  });

  stages.push({
    badge: "RAZORPAY → DECISION INTELLIGENCE",
    badgeClass: "razorpay",
    title: "05 · Verification",
    semantic: verification ? statusClass(verification.result) : "pending",
    lines: verification
      ? [verification.result, "Independently re-read from Razorpay -- never trusted from the write response alone."]
      : ["This action has not been verified yet."],
    detailRows: verification
      ? [["result", verification.result], ["reason", verification.reason || "—"], ["read attempts", verification.attempt_count]]
      : [],
  });

  stages.push({
    badge: "RESULT",
    badgeClass: outcome ? "success" : "engine",
    title: "06 · Outcome",
    semantic: outcome ? "success" : "pending",
    lines: outcome
      ? [`${formatAmount(outcome.recovered_amount, "")} recovered`, "Confirmed independently -- not assumed from the action alone."]
      : ["No recovered-amount outcome recorded (not a successful capture)."],
    detailRows: outcome && outcome.time_to_resolution_seconds !== undefined ? [["time to resolution", `${outcome.time_to_resolution_seconds.toFixed(1)}s`]] : [],
  });

  return stages;
}

function paymentAttemptSemantic(attempt) {
  return PAYMENT_ATTEMPT_STATUS_SEMANTICS[attempt.status] || "neutral";
}

function decisionPlainEnglish(decisionType) {
  if (decisionType === "RECOMMEND_CAPTURE") return "The engine recommends capturing the authorized payment.";
  if (decisionType === "RECOMMEND_RETRY_PROMPT") return "The engine recommends prompting the customer to retry.";
  if (decisionType === "NO_ACTION") return "The engine found nothing actionable in this observation.";
  return `The engine recorded ${decisionType}.`;
}

function policyPlainEnglish(policy) {
  if (policy.allowed === false) return "This merchant's policy blocks automatic action for a payment of this size.";
  if (policy.requires_approval) return "This payment requires merchant approval before any action is taken.";
  return "This merchant allows automatic capture for a payment of this size.";
}

async function runLiveRecovery() {
  const runButtons = qsa(".js-run-recovery");
  const statusEl = document.getElementById("recovery-status");
  const errorBox = document.getElementById("recovery-error");
  const container = document.getElementById("recovery-pipeline");

  runButtons.forEach((b) => { b.disabled = true; });
  hide(errorBox);
  clear(container);
  document.getElementById("recovery").scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });

  statusEl.textContent = "Fetching live order state…";
  let timeline;
  try {
    timeline = await Api.orderTimeline(SHOWCASE_ORDER_ID);
  } catch (err) {
    statusEl.textContent = "";
    errorBox.textContent =
      `Could not reach the live order (${err.message}). This section only ever shows real API data -- nothing is faked when it's unavailable.`;
    show(errorBox);
    runButtons.forEach((b) => { b.disabled = false; });
    return;
  }

  statusEl.textContent = "Calling the live reconcile endpoint (POST .../reconcile)…";
  try {
    await Api.reconcile(timeline.order.merchant_id, SHOWCASE_ORDER_ID);
  } catch (err) {
    statusEl.textContent = `Reconcile call failed (${err.message}) -- showing the last-known real state.`;
  }

  let finalTimeline = timeline;
  try {
    finalTimeline = await Api.orderTimeline(SHOWCASE_ORDER_ID);
  } catch (_) {
    // keep the timeline we already fetched
  }

  if (statusEl.textContent.indexOf("failed") === -1) statusEl.textContent = "";
  await revealPipelineStages(container, buildLiveStages(finalTimeline));
  runButtons.forEach((b) => { b.disabled = false; });
}

// ---------------------------------------------------------------------------
// "Try a scenario" -- calls the real /decision-lab/simulate endpoint,
// which runs the actual RuleBasedEngine + policy engine against a
// synthetic input. No business rule is reimplemented in this file.
// Action/Verification are always shown as not executed here: a
// simulation never moves money and never calls Razorpay.
// ---------------------------------------------------------------------------

const LAB_PRESETS = {
  capture: { amount: 500, status: "authorized", autoLimit: 5000, approvalLimit: 10000 },
  approval: { amount: 7000, status: "authorized", autoLimit: 5000, approvalLimit: 10000 },
  blocked: { amount: 15000, status: "authorized", autoLimit: 5000, approvalLimit: 10000 },
  already: { amount: 500, status: "captured", autoLimit: 5000, approvalLimit: 10000 },
};

function applyLabPreset(name) {
  const preset = LAB_PRESETS[name];
  if (!preset) return;
  document.getElementById("lab-amount").value = preset.amount;
  document.getElementById("lab-status").value = preset.status;
  document.getElementById("lab-auto-limit").value = preset.autoLimit;
  document.getElementById("lab-approval-limit").value = preset.approvalLimit;
}

function buildLabStages(result, input) {
  const amountText = formatRupees(input.amount);
  const decision = result.decision;
  const policy = result.policy;
  const stages = [];

  stages.push({
    badge: "SIMULATION INPUT",
    badgeClass: "razorpay",
    title: "01 · Payment (hypothetical)",
    semantic: "neutral",
    simulated: true,
    lines: [`${amountText} — ${input.status.toUpperCase()}`, "A synthetic input, not a real Razorpay payment."],
    detailRows: [["status", input.status], ["amount (paise)", input.amount]],
  });

  stages.push({
    badge: "DECISION INTELLIGENCE",
    badgeClass: "engine",
    title: "02 · Decision",
    semantic: "neutral",
    lines: [decision.decision_type, decisionPlainEnglish(decision.decision_type)],
    detailRows: [["model", decision.model_version], ["confidence", decision.confidence.toFixed(3)], ["reason codes", decision.reason_codes.join(", ")]],
  });

  stages.push({
    badge: "MERCHANT POLICY",
    badgeClass: "policy",
    title: "03 · Policy",
    semantic: policy ? policySemantic(policy) : "pending",
    lines: policy
      ? [policyOutcomeText(policy), policyPlainEnglish(policy)]
      : [result.policy_skipped_reason || "Not policy-gated."],
    detailRows: policy
      ? [
          ["authority_level_granted", policy.authority_level_granted],
          ["requires_approval", policy.requires_approval],
          ["allowed", policy.allowed],
          ["reason codes", policy.reason_codes.join(", ")],
        ]
      : [],
  });

  const wouldAct = policy && policy.allowed;
  stages.push({
    badge: "NOT EXECUTED",
    badgeClass: "muted",
    title: "04 · Action",
    semantic: "pending",
    simulated: true,
    lines: [
      "Simulation only -- no request was sent to Razorpay.",
      wouldAct ? "If live, this would attempt CAPTURE_PAYMENT." : "Policy did not allow an action, so none would execute.",
    ],
  });

  stages.push({
    badge: "NOT EXECUTED",
    badgeClass: "muted",
    title: "05 · Verification",
    semantic: "pending",
    simulated: true,
    lines: ["Simulation only -- nothing was read back from Razorpay."],
  });

  let outcomeLine;
  if (!policy) outcomeLine = "No money-moving action is proposed for this state.";
  else if (policy.allowed && !policy.requires_approval) outcomeLine = `Would recover ${amountText} if this were live.`;
  else if (policy.allowed && policy.requires_approval) outcomeLine = "Would wait for merchant approval -- no automatic money movement.";
  else outcomeLine = "Would remain blocked -- no money movement.";

  stages.push({
    badge: "SIMULATED RESULT",
    badgeClass: "muted",
    title: "06 · Outcome",
    semantic: "pending",
    simulated: true,
    lines: [outcomeLine, "Decision simulation — no money movement."],
  });

  return stages;
}

async function runScenario() {
  const errorBox = document.getElementById("lab-error");
  const container = document.getElementById("lab-pipeline");
  const runBtn = document.getElementById("lab-run-btn");

  const amountRupees = parseFloat(document.getElementById("lab-amount").value);
  const status = document.getElementById("lab-status").value;
  const autoLimitRupees = parseFloat(document.getElementById("lab-auto-limit").value);
  const approvalLimitRupees = parseFloat(document.getElementById("lab-approval-limit").value);

  if ([amountRupees, autoLimitRupees, approvalLimitRupees].some((n) => Number.isNaN(n) || n < 0)) {
    errorBox.textContent = "Enter non-negative numbers for amount and limits.";
    show(errorBox);
    return;
  }

  const input = {
    amount: Math.round(amountRupees * 100),
    status,
    auto_capture_limit: Math.round(autoLimitRupees * 100),
    approval_limit: Math.round(approvalLimitRupees * 100),
  };

  hide(errorBox);
  runBtn.disabled = true;
  clear(container);

  let result;
  try {
    result = await Api.simulate(input);
  } catch (err) {
    errorBox.textContent = `Simulation failed: ${err.message}`;
    show(errorBox);
    runBtn.disabled = false;
    return;
  }

  await revealPipelineStages(container, buildLabStages(result, input));
  runBtn.disabled = false;
}

function initLab() {
  document.getElementById("lab-run-btn").addEventListener("click", runScenario);
  qsa(".chip-btn[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyLabPreset(btn.dataset.preset);
      runScenario();
    });
  });
}

// ---------------------------------------------------------------------------
// Hero payment object + Live proof section -- both read the same real
// showcase timeline.
// ---------------------------------------------------------------------------

async function initHeroAndProof() {
  const heroAmount = document.getElementById("hero-payment-object");
  let timeline;
  try {
    timeline = await fetchShowcaseTimeline();
  } catch (err) {
    if (heroAmount) heroAmount.classList.add("razorpay-object-unavailable");
    qs('[data-el="hero-status"]').textContent = "unavailable";
    qs('[data-el="hero-amount"]').textContent = "—";
    renderProofUnavailable(err);
    return;
  }

  populateHeroPaymentObject(timeline);
  renderProofPanel(timeline);
}

function populateHeroPaymentObject(timeline) {
  const order = timeline.order;
  const latestAttempt = timeline.payment_attempts.length
    ? timeline.payment_attempts[timeline.payment_attempts.length - 1]
    : null;

  qs('[data-el="hero-amount"]').textContent = formatAmount(order.amount, order.currency);
  qs('[data-el="hero-status"]').textContent = latestAttempt ? latestAttempt.status.toUpperCase() : "NO PAYMENT YET";
  qs('[data-el="hero-captured"]').textContent = latestAttempt ? `CAPTURED: ${latestAttempt.captured ? "YES" : "NO"}` : "";
  qs('[data-el="hero-order-id"]').textContent = order.id;
  qs('[data-el="hero-payment-id"]').textContent = latestAttempt ? latestAttempt.id : "—";
}

function renderProofUnavailable(err) {
  const loading = document.getElementById("proof-loading");
  const errorBox = document.getElementById("proof-error");
  hide(loading);
  errorBox.textContent =
    `Live proof data isn't reachable right now (${err.message}). This section reads a real order from the live ` +
    "API rather than embedding a fixed result.";
  show(errorBox);
}

function renderProofPanel(timeline) {
  const loading = document.getElementById("proof-loading");
  const errorBox = document.getElementById("proof-error");
  const card = document.getElementById("proof-card");
  hide(loading);
  hide(errorBox);
  clear(card);

  const order = timeline.order;
  const latestAttempt = timeline.payment_attempts.length
    ? timeline.payment_attempts[timeline.payment_attempts.length - 1]
    : null;
  const verification = timeline.verification;
  const outcome = timeline.outcome;

  const grid = el("div", "proof-grid");
  const field = (label, value) => {
    const box = el("div", "proof-field");
    box.appendChild(el("span", "proof-field-label", label));
    box.appendChild(el("span", "proof-field-value", value === null || value === undefined || value === "" ? "—" : String(value)));
    return box;
  };
  grid.appendChild(field("Order ID", order.id));
  grid.appendChild(field("Payment ID", latestAttempt ? latestAttempt.id : "—"));
  grid.appendChild(field("Amount", formatAmount(order.amount, order.currency)));
  grid.appendChild(field("Final captured state", latestAttempt ? (latestAttempt.captured ? "true" : "false") : "—"));
  grid.appendChild(field("Verification result", verification ? verification.result : "Not reached"));
  grid.appendChild(field("Recovered amount", outcome ? formatAmount(outcome.recovered_amount, "") : "Not available"));
  card.appendChild(grid);

  const link = el("a", "proof-link", "View the full audit trail in the console →");
  link.href = `#order/${encodeURIComponent(SHOWCASE_ORDER_ID)}`;
  card.appendChild(link);

  show(card);
}

// ---------------------------------------------------------------------------
// Audit trail proof chain (landing page)
// ---------------------------------------------------------------------------

const CHECKPOINT_INFO = {
  EVENT_INGESTED: "A new payment-state change was recorded from Razorpay.",
  DECISION_CREATED: "The engine recorded its recommendation.",
  POLICY_EVALUATED: "Merchant policy decided whether the action may proceed.",
  ACTION_AUTHORIZED: "The action was cleared to execute.",
  ACTION_EXECUTED: "The write call to Razorpay was made.",
  VERIFICATION_COMPLETED: "Razorpay was independently re-read to confirm the outcome.",
  ACTION_BLOCKED: "Policy prevented this action from executing.",
  APPROVAL_PENDING: "This action is waiting on merchant approval.",
  APPROVAL_GRANTED: "A merchant approved a pending action.",
  RECONCILIATION_ANOMALY: "An unexpected state transition was recorded rather than guessed at.",
};

async function initAuditProof() {
  const list = document.getElementById("proof-audit");
  if (!list) return;
  let timeline;
  try {
    timeline = await fetchShowcaseTimeline();
  } catch (err) {
    list.appendChild(el("li", "audit-proof-empty", "Audit trail unavailable -- live data could not be reached."));
    return;
  }
  if (!timeline.audit || timeline.audit.length === 0) {
    list.appendChild(el("li", "audit-proof-empty", "No audit checkpoints recorded yet for this order."));
    return;
  }
  timeline.audit.forEach((entry, i) => {
    const item = el("li", "audit-proof-item");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "audit-proof-head";
    btn.appendChild(el("span", "audit-proof-num", String(i + 1).padStart(2, "0")));
    btn.appendChild(el("span", "audit-proof-checkpoint", entry.checkpoint));
    item.appendChild(btn);

    const body = el("div", "audit-proof-body hidden");
    body.appendChild(el("p", "audit-proof-plain", CHECKPOINT_INFO[entry.checkpoint] || "A checkpoint was recorded."));
    const pre = document.createElement("pre");
    pre.className = "audit-proof-payload";
    pre.textContent = JSON.stringify(entry.snapshot, null, 2);
    body.appendChild(pre);
    item.appendChild(body);

    btn.addEventListener("click", () => body.classList.toggle("hidden"));
    list.appendChild(item);
  });
}

// ---------------------------------------------------------------------------
// AI judgment -- interactive reasons
// ---------------------------------------------------------------------------

const JUDGMENT_REASON_TEXT = {
  explain: "Every decision carries the exact reason codes that produced it -- nothing is a black box.",
  deterministic: "The same observed state always yields the same decision, every time.",
  policy: "Merchant-configured limits gate every action, unconditionally -- the engine can only recommend.",
  audit: "Every step is written to an append-only audit trail, not reconstructed after the fact.",
  verify: "No action is trusted without independently re-confirming it against Razorpay.",
};

function initJudgmentReasons() {
  const detail = document.getElementById("judgment-reason-detail");
  qsa(".judgment-reason").forEach((btn) => {
    btn.addEventListener("click", () => {
      qsa(".judgment-reason").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      if (detail) detail.textContent = JUDGMENT_REASON_TEXT[btn.dataset.reason] || "";
    });
  });
}

// ---------------------------------------------------------------------------
// Failure recovery -- interactive stepper
// ---------------------------------------------------------------------------

const INCIDENT_STEPS = [
  { label: "Expected", text: "A fresh Test Mode order should land in AUTHORIZED / captured=false, ready for the pipeline to act on." },
  { label: "First test", text: "Instead, an initial transaction reached captured before our reconciliation flow ever observed it as authorized -- there was nothing left to act on." },
  { label: "Investigate", text: "We checked, in order: the fetched payment state, how the order was created, the Checkout configuration used to pay it, the read-client adapter's invocation, and reconciliation's status-to-event mapping." },
  { label: "Isolate", text: "Our code never captured anything -- the read-only client has no write method, and the write path is never invoked by reconciliation. The order itself reached Razorpay already captured, most likely created without an explicit manual-capture configuration." },
  { label: "Fix", text: "We created a fresh Test Mode order with explicit manual capture." },
  { label: "Result", text: "It correctly landed in AUTHORIZED / captured=false. Our system then produced RECOMMEND_CAPTURE → ALLOW → CAPTURE_PAYMENT → VERIFIED_SUCCESS, and Razorpay independently confirmed captured=true." },
];

let incidentIndex = 0;

function initIncidentStepper() {
  const stepsWrap = document.getElementById("incident-steps");
  const dotsWrap = document.getElementById("incident-dots");
  if (!stepsWrap || !dotsWrap) return;

  INCIDENT_STEPS.forEach((step, i) => {
    const panel = el("div", "incident-step");
    panel.appendChild(el("span", "incident-step-label", step.label));
    panel.appendChild(el("p", "incident-step-text", step.text));
    stepsWrap.appendChild(panel);

    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "incident-dot";
    dot.setAttribute("aria-label", `Step ${i + 1}: ${step.label}`);
    dot.addEventListener("click", () => showIncidentStep(i));
    dotsWrap.appendChild(dot);
  });

  const prevBtn = document.getElementById("incident-prev");
  const nextBtn = document.getElementById("incident-next");
  if (prevBtn) prevBtn.addEventListener("click", () => showIncidentStep(Math.max(0, incidentIndex - 1)));
  if (nextBtn) nextBtn.addEventListener("click", () => showIncidentStep(Math.min(INCIDENT_STEPS.length - 1, incidentIndex + 1)));

  showIncidentStep(0);
}

function showIncidentStep(i) {
  incidentIndex = i;
  qsa(".incident-step").forEach((p, idx) => p.classList.toggle("is-active", idx === i));
  qsa(".incident-dot").forEach((d, idx) => d.classList.toggle("is-active", idx === i));
  const prevBtn = document.getElementById("incident-prev");
  const nextBtn = document.getElementById("incident-next");
  if (prevBtn) prevBtn.disabled = i === 0;
  if (nextBtn) nextBtn.disabled = i === INCIDENT_STEPS.length - 1;
}

// ---------------------------------------------------------------------------
// Landing init
// ---------------------------------------------------------------------------

function initLanding() {
  document.getElementById("hero-run-btn").classList.add("js-run-recovery");
  document.getElementById("recovery-run-btn").classList.add("js-run-recovery");
  qsa(".js-run-recovery").forEach((btn) => btn.addEventListener("click", runLiveRecovery));

  initLab();
  initJudgmentReasons();
  initIncidentStepper();
  initHeroAndProof();
  initAuditProof();
}

// ---------------------------------------------------------------------------
// View switching (hash-based, no router library)
// ---------------------------------------------------------------------------

function navigateToOrder(orderId) {
  window.location.hash = `#order/${encodeURIComponent(orderId)}`;
}

function navigateToConsole() {
  window.location.hash = "#console";
}

function currentRoute() {
  const hash = window.location.hash.replace(/^#/, "");
  if (hash.startsWith("order/")) {
    return { view: "order", orderId: decodeURIComponent(hash.slice("order/".length)) };
  }
  if (hash === "console") {
    return { view: "console" };
  }
  return { view: "landing" };
}

async function router() {
  const route = currentRoute();
  const landingView = document.getElementById("landing-view");
  const dashboardView = document.getElementById("dashboard-view");
  const detailView = document.getElementById("detail-view");

  if (route.view === "order") {
    hide(landingView);
    hide(dashboardView);
    show(detailView);
    window.scrollTo(0, 0);
    await renderOrderDetail(route.orderId);
  } else if (route.view === "console") {
    hide(landingView);
    hide(detailView);
    show(dashboardView);
    window.scrollTo(0, 0);
    await renderDashboard();
  } else {
    hide(dashboardView);
    hide(detailView);
    show(landingView);
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.getElementById("back-to-dashboard").addEventListener("click", navigateToConsole);
window.addEventListener("hashchange", router);

refreshHealth();
initLanding();
router();
