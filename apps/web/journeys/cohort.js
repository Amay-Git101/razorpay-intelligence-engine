"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { renderFailurePattern } from "../lib/pattern.js";
import {
  clear,
  el,
  notice,
  paymentTone,
  pill,
  primaryButton,
  rupees,
  secondaryButton,
  statusLine,
  step,
} from "../lib/ui.js";

/**
 * Problem 03 -- is this one payment failing, or are many?
 *
 * Six real Razorpay Test Mode orders are created up front, and the cohort
 * is frozen at that moment. Whatever the evaluator then does to them --
 * pay some, fail some, leave some untouched -- is what gets counted. The
 * conclusion is computed from those six and nothing else.
 *
 * The only state kept in the browser is which experiment is being run. Every
 * payment state comes from the server on each refresh, so reloading the page
 * mid-experiment loses nothing.
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

function reset() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (_) {
    /* ignore */
  }
}

function paymentStateLabel(order) {
  if (!order.payment_status) return "Not started";
  if (order.payment_status === "captured") return "Succeeded";
  if (order.payment_status === "authorized") return order.payment_captured ? "Captured" : "Authorized";
  if (order.payment_status === "failed") return "Failed";
  return order.payment_status;
}

export function renderCohortJourney(container, ctx) {
  let state = load() || {};
  let cohort = null;
  let pattern = null;
  let busyOrder = null;

  const createStatus = statusLine();
  const analyzeStatus = statusLine();

  async function refreshCohort() {
    if (!state.experimentId) return;
    cohort = await Api.experiment(state.experimentId);
  }

  function observedCount() {
    if (!cohort) return 0;
    return cohort.orders.filter((o) => o.payment_status).length;
  }

  function rerender() {
    save(state);
    clear(container);
    draw();
  }

  function draw() {
    // ---- Step 1 ----
    const createStep = step({
      number: 1,
      title: `Create ${COHORT_SIZE} test payments`,
      state: state.experimentId ? "done" : "active",
    });

    if (!state.experimentId) {
      createStep.content.appendChild(
        el("p", "lede", `This creates ${COHORT_SIZE} real orders in Razorpay Test Mode and fixes them as the group to be judged. Fixing the group first is what makes "4 of 6" mean something later.`),
      );
      createStep.content.appendChild(primaryButton(`Create ${COHORT_SIZE} test orders`, createCohort));
      createStep.content.appendChild(createStatus.node);
    } else {
      createStep.content.appendChild(
        el("p", "done-line", `${COHORT_SIZE} real orders created. This group is fixed — the denominator cannot move.`),
      );
    }
    container.appendChild(createStep.node);

    if (!state.experimentId || !cohort) return;

    // ---- Step 2 ----
    const payStep = step({ number: 2, title: "Pay them — succeed some, fail some", state: "active" });
    payStep.content.appendChild(
      el("p", "lede", "Each one opens Razorpay Checkout. To make a payment fail, choose a failure option inside Razorpay's own dialog. Leave some unpaid if you like — they stay in the count as unpaid."),
    );

    const grid = el("div", "cohort-grid");
    cohort.orders.forEach((order) => {
      const card = el("div", `cohort-card cohort-${order.payment_status ? paymentTone(order.payment_status) : "idle"}`);
      card.appendChild(el("span", "cohort-position", `Payment ${order.position}`));
      card.appendChild(el("span", "cohort-amount", rupees(order.amount)));
      card.appendChild(pill(paymentStateLabel(order), order.payment_status ? paymentTone(order.payment_status) : "neutral"));
      if (order.error_reason) {
        card.appendChild(el("span", "cohort-error", order.error_reason));
      }
      card.appendChild(el("span", "cohort-order-id", order.order_id));

      if (!order.payment_status) {
        const button = primaryButton(busyOrder === order.order_id ? "Working…" : "Pay", () => payOne(order));
        button.disabled = busyOrder !== null;
        button.classList.add("btn-small");
        card.appendChild(button);
      }
      grid.appendChild(card);
    });
    payStep.content.appendChild(grid);
    container.appendChild(payStep.node);

    // ---- Step 3 ----
    const analyzeStep = step({ number: 3, title: "Ask whether this is one failure or a pattern", state: "active" });
    const observed = observedCount();
    analyzeStep.content.appendChild(
      el("p", "lede", `${observed} of ${cohort.orders.length} payments in this group have a result so far.`),
    );
    analyzeStep.content.appendChild(primaryButton("Analyse this group", analyze));
    analyzeStep.content.appendChild(analyzeStatus.node);
    if (pattern) analyzeStep.content.appendChild(renderFailurePattern(pattern));

    const actions = el("div", "actions");
    actions.appendChild(
      secondaryButton("Start a new group", () => {
        reset();
        state = {};
        cohort = null;
        pattern = null;
        rerender();
      }),
    );
    analyzeStep.content.appendChild(actions);
    container.appendChild(analyzeStep.node);
  }

  async function createCohort() {
    createStatus.set(`Creating ${COHORT_SIZE} real orders in Razorpay Test Mode…`);
    try {
      const result = await Api.createTestOrders(ctx.merchant.id, {
        kind: "failure_pattern",
        count: COHORT_SIZE,
        amount: AMOUNT,
        label: "one payment or many",
      });
      state = { experimentId: result.experiment_id };
      await refreshCohort();
      createStatus.clear();
      rerender();
    } catch (error) {
      createStatus.clear();
      container.appendChild(notice(`The orders could not be created: ${error.message}`, "bad"));
    }
  }

  async function payOne(order) {
    busyOrder = order.order_id;
    rerender();
    try {
      await openCheckout({
        order: { order_id: order.order_id, amount: order.amount, currency: order.currency },
        onClosed: async () => {
          try {
            // However Checkout ended, ask the server what Razorpay says.
            await Api.reconcile(ctx.merchant.id, order.order_id);
          } catch (_) {
            // A reconcile failure leaves this order without an observed
            // state, which the cohort will show as "not started" rather
            // than guessing at an outcome.
          }
          await refreshCohort();
          busyOrder = null;
          pattern = null;
          rerender();
        },
      });
    } catch (error) {
      busyOrder = null;
      rerender();
      container.appendChild(notice(error.message, "bad"));
    }
  }

  async function analyze() {
    analyzeStatus.set("Counting the results in this group…");
    try {
      await refreshCohort();
      pattern = await Api.experimentFailurePattern(state.experimentId);
      analyzeStatus.clear();
      rerender();
    } catch (error) {
      analyzeStatus.clear();
      container.appendChild(notice(`The group could not be analysed: ${error.message}`, "bad"));
    }
  }

  (async () => {
    if (state.experimentId) {
      try {
        await refreshCohort();
      } catch (_) {
        // The stored experiment is gone; start clean rather than showing a
        // cohort that no longer exists.
        reset();
        state = {};
      }
    }
    rerender();
  })();
}
