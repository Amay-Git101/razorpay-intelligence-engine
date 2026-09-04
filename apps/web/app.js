"use strict";

/**
 * Decision Intelligence Console -- vanilla JS, no build step, no
 * framework. Talks only to the existing HTTP API via relative fetch()
 * calls (same origin, no hardcoded host/port, no CORS needed).
 *
 * Renders exactly what the API returns. A field the API sends as null
 * is rendered as an explicit "Not reached" / "Not available" state --
 * never as a fabricated success. Status-to-color mapping below mirrors
 * the semantic grouping already established by the backend's own
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

// ---------------------------------------------------------------------------
// Application state
// ---------------------------------------------------------------------------

const state = {
  merchantId: null,
  merchantName: null,
};

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
// Dashboard
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
// Order detail
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
// Reconcile interaction
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
// View switching (hash-based, no router library)
// ---------------------------------------------------------------------------

function navigateToOrder(orderId) {
  window.location.hash = `#order/${encodeURIComponent(orderId)}`;
}

function navigateToDashboard() {
  window.location.hash = "";
}

function currentRoute() {
  const hash = window.location.hash.replace(/^#/, "");
  if (hash.startsWith("order/")) {
    return { view: "order", orderId: decodeURIComponent(hash.slice("order/".length)) };
  }
  return { view: "dashboard" };
}

async function router() {
  const route = currentRoute();
  const dashboardView = document.getElementById("dashboard-view");
  const detailView = document.getElementById("detail-view");

  if (route.view === "order") {
    hide(dashboardView);
    show(detailView);
    await renderOrderDetail(route.orderId);
  } else {
    hide(detailView);
    show(dashboardView);
    await renderDashboard();
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.getElementById("back-to-dashboard").addEventListener("click", navigateToDashboard);
window.addEventListener("hashchange", router);

refreshHealth();
router();
