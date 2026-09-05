"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { actionButton, createEventStream, focusOn } from "../lib/live.js";
import { renderFailurePattern } from "../lib/pattern.js";
import { openTestDetails, testDetailsLink } from "../lib/testcards.js";
import { clear, el, notice, primaryButton, rupees, secondaryButton } from "../lib/ui.js";

/**
 * Problem 03 -- is this one payment failing, or are many?
 *
 * The six orders are created one real call at a time, and each card
 * appears when its own order actually exists at Razorpay. Nothing is
 * rendered ahead of the backend: the counter cannot reach 4/6 unless four
 * orders have genuinely been created.
 *
 * After that the evaluator drives the outcomes. The group summary is
 * recomputed from the server's own view of the cohort after every payment,
 * and the conclusion at the end is the backend's, not this file's.
 */

const STORAGE_KEY = "journey.cohort";
const COHORT_SIZE = 6;
const AMOUNT = 50000;

function load() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (_) {
    return null;
  }
}

function save(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (_) {
    /* ignore */
  }
}

function stateLabel(order) {
  if (!order.payment_status) return { text: "Not started", tone: "idle" };
  if (order.payment_status === "captured") return { text: "Succeeded", tone: "good" };
  if (order.payment_status === "authorized") {
    return { text: order.payment_captured ? "Captured" : "Authorized", tone: "attention" };
  }
  if (order.payment_status === "failed") return { text: "Failed", tone: "bad" };
  return { text: order.payment_status, tone: "neutral" };
}

export function renderCohortJourney(container, ctx) {
  let state = load() || {};
  let cohort = null;
  let pattern = null;
  let busyOrder = null;

  const stream = createEventStream();

  const createArea = el("section", "action-area");
  const gridArea = el("section", "cohort-area");
  const analyseArea = el("section", "result-area");

  container.appendChild(createArea);
  container.appendChild(stream.node);
  container.appendChild(gridArea);
  container.appendChild(analyseArea);

  function persist() {
    save(state);
  }

  async function refreshCohort() {
    cohort = await Api.experiment(state.experimentId);
  }

  function counts() {
    if (!cohort) return { observed: 0, succeeded: 0, failed: 0, untested: 0, total: 0 };
    const orders = cohort.orders;
    return {
      total: orders.length,
      observed: orders.filter((o) => o.payment_status).length,
      succeeded: orders.filter((o) => o.payment_status === "captured" || (o.payment_status === "authorized")).length,
      failed: orders.filter((o) => o.payment_status === "failed").length,
      untested: orders.filter((o) => !o.payment_status).length,
    };
  }

  // ---------------------------------------------------------------------
  // Step 1 -- create six real orders, one at a time
  // ---------------------------------------------------------------------

  function renderCreate() {
    clear(createArea);

    if (state.experimentId) {
      const done = el("p", "action-done", `${COHORT_SIZE} real Test Mode orders created. This group is now fixed — the denominator cannot move.`);
      createArea.appendChild(done);
      return;
    }

    createArea.appendChild(el("h2", "action-title", `Create ${COHORT_SIZE} real test payments`));
    createArea.appendChild(
      el("p", "action-lede", "Each one is a real Razorpay Test Mode order. They are fixed as a group before any of them is paid, so the result at the end is four of these six — not four of whatever happens to exist later."),
    );

    const progress = el("div", "create-progress");
    progress.hidden = true;
    const counter = el("span", "create-counter", `0 / ${COHORT_SIZE}`);
    progress.appendChild(counter);
    const bar = el("div", "create-bar");
    const fill = el("div", "create-bar-fill");
    bar.appendChild(fill);
    progress.appendChild(bar);

    const button = actionButton({
      label: `Create ${COHORT_SIZE} test orders`,
      workingLabel: "Creating…",
      onClick: () => createCohort(progress, counter, fill),
    });
    createArea.appendChild(button);
    createArea.appendChild(progress);
  }

  async function createCohort(progress, counter, fill) {
    progress.hidden = false;
    let experimentId = null;
    const created = [];

    for (let i = 1; i <= COHORT_SIZE; i += 1) {
      try {
        const result = await Api.createTestOrders(ctx.merchant.id, {
          kind: "failure_pattern",
          count: 1,
          amount: AMOUNT,
          label: "one payment or many",
          experiment_id: experimentId,
        });
        experimentId = result.experiment_id;
        created.push(result.orders[0]);

        // The counter only moves because an order now genuinely exists.
        counter.textContent = `${i} / ${COHORT_SIZE}`;
        fill.style.width = `${(i / COHORT_SIZE) * 100}%`;
        stream.push(`Order ${i} created — ${result.orders[0].order_id}.`, "good");

        state.experimentId = experimentId;
        persist();
        await refreshCohort();
        renderGrid();
      } catch (error) {
        stream.push(`Order ${i} could not be created: ${error.message}`, "bad");
        clear(analyseArea);
        analyseArea.appendChild(
          notice(
            `Only ${created.length} of ${COHORT_SIZE} orders were created. The ones that exist are real and still usable; you can carry on with them.`,
            "bad",
          ),
        );
        break;
      }
    }

    renderCreate();
    renderGrid();
    renderAnalyse();
    focusOn(gridArea);
  }

  // ---------------------------------------------------------------------
  // Step 2 -- the evaluator creates the pattern
  // ---------------------------------------------------------------------

  function renderGrid() {
    clear(gridArea);
    if (!cohort || !cohort.orders.length) return;

    const head = el("div", "cohort-head");
    const titles = el("div");
    titles.appendChild(el("h2", "action-title", "Now create the pattern yourself"));
    titles.appendChild(
      el("p", "action-lede", "Pay some and fail some — in Razorpay's own Checkout, an OTP shorter than 4 digits fails the payment. Leaving some unpaid is fine; they stay in the count."),
    );
    head.appendChild(titles);
    head.appendChild(testDetailsLink());
    gridArea.appendChild(head);

    // Live summary, recomputed from the server's view of the cohort.
    const c = counts();
    const summary = el("div", "cohort-summary");
    const stat = (label, value, tone) => {
      const box = el("div", `summary-stat${tone ? ` summary-${tone}` : ""}`);
      box.appendChild(el("span", "summary-value", String(value)));
      box.appendChild(el("span", "summary-label", label));
      return box;
    };
    summary.appendChild(stat("with a result", `${c.observed} / ${c.total}`));
    summary.appendChild(stat("succeeded", c.succeeded, c.succeeded ? "good" : null));
    summary.appendChild(stat("failed", c.failed, c.failed ? "bad" : null));
    summary.appendChild(stat("not yet tested", c.untested));
    gridArea.appendChild(summary);

    const grid = el("div", "cohort-grid");
    cohort.orders.forEach((order) => {
      const label = stateLabel(order);
      const card = el("div", `cohort-card cohort-${label.tone}`);
      card.appendChild(el("span", "cohort-position", `Payment ${order.position}`));
      card.appendChild(el("span", "cohort-amount", rupees(order.amount)));

      const status = el("span", `cohort-state cohort-state-${label.tone}`);
      status.textContent = label.text;
      card.appendChild(status);

      if (order.error_reason) card.appendChild(el("span", "cohort-error", order.error_reason));
      card.appendChild(el("span", "cohort-order-id", order.order_id));

      if (!order.payment_status) {
        const pay = el("button", "btn btn-primary btn-small", busyOrder === order.order_id ? "Working…" : "Pay");
        pay.type = "button";
        pay.disabled = busyOrder !== null;
        pay.addEventListener("click", () => payOne(order));
        card.appendChild(pay);

        const help = el("button", "link-btn link-btn-small", "Test details");
        help.type = "button";
        help.addEventListener("click", () => openTestDetails(`Payment ${order.position}`));
        card.appendChild(help);
      }
      grid.appendChild(card);
    });
    gridArea.appendChild(grid);
  }

  async function payOne(order) {
    busyOrder = order.order_id;
    renderGrid();
    stream.push(`Opening Checkout for payment ${order.position}…`);

    try {
      await openCheckout({
        order: { order_id: order.order_id, amount: order.amount, currency: order.currency },
        onClosed: async () => {
          try {
            stream.push(`Asking Razorpay what happened to payment ${order.position}…`);
            await Api.reconcile(ctx.merchant.id, order.order_id);
          } catch (_) {
            // Leaves this one without an observed state, which the grid
            // shows as "not started" rather than guessing an outcome.
            stream.push(`Payment ${order.position} could not be read back yet.`, "bad");
          }
          await refreshCohort();

          const updated = cohort.orders.find((o) => o.order_id === order.order_id);
          if (updated && updated.payment_status) {
            const label = stateLabel(updated);
            stream.push(
              `Payment ${order.position}: ${label.text.toLowerCase()}${updated.error_reason ? ` — ${updated.error_reason}` : ""}.`,
              label.tone === "bad" ? "bad" : "good",
            );
          }

          busyOrder = null;
          pattern = null;
          renderGrid();
          renderAnalyse();
        },
      });
    } catch (error) {
      busyOrder = null;
      renderGrid();
      stream.push(error.message, "bad");
    }
  }

  // ---------------------------------------------------------------------
  // Step 3 -- what does the group say?
  // ---------------------------------------------------------------------

  function renderAnalyse() {
    clear(analyseArea);
    if (!cohort || !cohort.orders.length) return;

    const c = counts();
    analyseArea.appendChild(el("h2", "action-title", "Ask what this group means"));

    if (c.observed === 0) {
      analyseArea.appendChild(
        notice("Pay at least one of them first — there is nothing to count yet.", "neutral"),
      );
      return;
    }

    analyseArea.appendChild(
      el("p", "action-lede", `${c.observed} of ${c.total} payments in this group have a result. The backend decides whether that is enough to call anything.`),
    );
    analyseArea.appendChild(
      actionButton({
        label: "Analyse this group",
        workingLabel: "Counting…",
        onClick: analyse,
      }),
    );

    if (pattern) {
      const holder = el("div", "pattern-holder");
      holder.appendChild(renderFailurePattern(pattern));
      analyseArea.appendChild(holder);
    }

    const actions = el("div", "actions");
    actions.appendChild(
      secondaryButton("Start a new group", () => {
        state = {};
        cohort = null;
        pattern = null;
        persist();
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch (_) {
          /* ignore */
        }
        stream.clear();
        clear(gridArea);
        renderCreate();
        renderAnalyse();
        focusOn(createArea);
      }),
    );
    analyseArea.appendChild(actions);
  }

  async function analyse() {
    stream.push("Counting the results in this group…");
    try {
      await refreshCohort();
      pattern = await Api.experimentFailurePattern(state.experimentId);
      stream.push(`Backend conclusion: ${pattern.interpretation.headline}`, pattern.interpretation.consistent_with_wider_problem ? "bad" : "good");
      renderGrid();
      renderAnalyse();
      focusOn(analyseArea);
    } catch (error) {
      stream.push(`The group could not be analysed: ${error.message}`, "bad");
    }
  }

  // ---------------------------------------------------------------------

  (async () => {
    if (state.experimentId) {
      try {
        await refreshCohort();
      } catch (_) {
        state = {};
        persist();
      }
    }
    renderCreate();
    renderGrid();
    renderAnalyse();
  })();
}
