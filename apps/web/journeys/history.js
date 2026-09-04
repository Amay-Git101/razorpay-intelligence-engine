"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { decisionSentence } from "../lib/pipeline.js";
import {
  clear,
  el,
  fact,
  notice,
  pill,
  primaryButton,
  rupees,
  secondaryButton,
  statusLine,
  step,
  technicalDetails,
} from "../lib/ui.js";

/**
 * Problem 04 -- does the customer's previous payment behaviour change the
 * decision?
 *
 * The history is real: Razorpay's payment object carries the payer's email,
 * this system already stores that object, and prior payments are found by
 * matching on it. So the way to build a history here is the honest one --
 * pay more than once with the same email.
 *
 * The system never labels a payer. It counts their observed outcomes, and
 * those counts can only ever move a decision toward human review, never
 * toward more automation. That asymmetry is enforced in the engine, not
 * described here.
 */

const STORAGE_KEY = "journey.history";
const AMOUNT = 50000;
const DEFAULT_EMAIL = "returning.payer@example.com";

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

export function renderHistoryJourney(container, ctx) {
  let state = load() || { stage: "start", email: DEFAULT_EMAIL, rounds: 0 };
  const createStatus = statusLine();
  const runStatus = statusLine();

  function rerender() {
    save(state);
    clear(container);
    draw();
  }

  function draw() {
    // ---- Step 1: choose the payer ----
    const setupStep = step({
      number: 1,
      title: "Choose the payer",
      state: state.stage === "start" ? "active" : "done",
    });

    if (state.stage === "start") {
      setupStep.content.appendChild(
        el("p", "lede", "History is matched on the email Razorpay records with the payment. Use the same address each time you come back here and this payer accumulates a real history."),
      );
      const field = el("label", "field");
      field.appendChild(el("span", "field-label", "Payer email"));
      const input = el("input", "field-input");
      input.type = "email";
      input.value = state.email;
      field.appendChild(input);
      setupStep.content.appendChild(field);
      setupStep.content.appendChild(
        primaryButton("Create a test order for this payer", () => createOrder(input.value.trim() || DEFAULT_EMAIL)),
      );
      setupStep.content.appendChild(createStatus.node);
      if (state.rounds > 0) {
        setupStep.content.appendChild(
          notice(`You have completed ${state.rounds} payment(s) as this payer in this browser. Each one adds to the history the system reads.`, "neutral"),
        );
      }
    } else {
      setupStep.content.appendChild(el("p", "done-line", `Payer: ${state.email}. Order ${state.orderId} created for ${rupees(AMOUNT)}.`));
    }
    container.appendChild(setupStep.node);

    if (state.stage === "start") return;

    // ---- Step 2: pay ----
    const payStep = step({
      number: 2,
      title: "Pay as that customer",
      state: state.stage === "created" ? "active" : "done",
    });
    if (state.stage === "created") {
      payStep.content.appendChild(
        el("p", "lede", "Checkout opens with that email prefilled. Pay it, or fail it deliberately — both are useful history."),
      );
      payStep.content.appendChild(primaryButton("Pay with Razorpay", pay));
    } else {
      payStep.content.appendChild(el("p", "done-line", "Checkout closed."));
    }
    container.appendChild(payStep.node);

    if (state.stage === "created") return;

    // ---- Step 3: run it ----
    const runStep = step({
      number: 3,
      title: "Let the system look at this payment and the payer's past",
      state: state.stage === "paid" ? "active" : "done",
    });
    if (state.stage === "paid") {
      runStep.content.appendChild(primaryButton("Run the system", run));
      runStep.content.appendChild(runStatus.node);
    } else {
      runStep.content.appendChild(el("p", "done-line", "The pipeline ran and the payer's history was read."));
    }
    container.appendChild(runStep.node);

    if (state.stage !== "done") return;

    // ---- Step 4: result ----
    const resultStep = step({ number: 4, title: "Current payment, plus what came before", state: "active" });

    const columns = el("div", "history-columns");

    const current = el("div", "history-col");
    current.appendChild(el("h3", "sub-title", "This payment"));
    const attempt = state.timeline && state.timeline.payment_attempts.length
      ? state.timeline.payment_attempts[state.timeline.payment_attempts.length - 1]
      : null;
    if (attempt) {
      current.appendChild(fact("Amount", rupees(attempt.amount)));
      current.appendChild(fact("State", attempt.status));
      if (attempt.error_reason) current.appendChild(fact("Reported reason", attempt.error_reason));
    } else {
      current.appendChild(notice("No payment was observed for this order.", "neutral"));
    }
    columns.appendChild(current);

    const prior = el("div", "history-col");
    prior.appendChild(el("h3", "sub-title", "This payer's previous payments"));
    const history = state.history;
    if (!history) {
      prior.appendChild(notice("Not read yet.", "neutral"));
    } else if (!history.identity_available) {
      prior.appendChild(
        notice(
          "This payment carries nothing to recognise a payer by, so no history could be looked up. That is different from the payer having no history, and the system reports it differently.",
          "neutral",
        ),
      );
    } else if (history.history.prior_payment_count === 0) {
      prior.appendChild(
        notice("This payer has no earlier payments with this merchant. Come back and pay again with the same email to build one.", "neutral"),
      );
    } else {
      prior.appendChild(fact("Previous payments", history.history.prior_payment_count));
      prior.appendChild(fact("Succeeded", history.history.prior_captured_count));
      prior.appendChild(fact("Failed", history.history.prior_failed_count));
      prior.appendChild(fact("Across orders", history.history.distinct_prior_orders));
    }
    columns.appendChild(prior);
    resultStep.content.appendChild(columns);

    // ---- What the system did with it ----
    const decision = state.timeline && state.timeline.decision;
    if (decision) {
      const usedBox = el("div", "history-decision");
      usedBox.appendChild(el("h3", "sub-title", "What the system decided"));
      usedBox.appendChild(pill(decision.decision_type, "neutral"));
      usedBox.appendChild(el("p", "story-line", decisionSentence(decision.decision_type)));

      const historyCodes = (decision.reason_codes || []).filter((code) => code.startsWith("CUSTOMER_HISTORY") || code.startsWith("PRIOR_CUSTOMER"));
      usedBox.appendChild(
        el(
          "p",
          "story-line",
          historyCodes.length
            ? "The payer's history is part of the recorded reason for this decision:"
            : "The payer's history did not change this decision. It is recorded in the decision's context either way.",
        ),
      );
      if (historyCodes.length) {
        const chips = el("div", "chips");
        historyCodes.forEach((code) => chips.appendChild(el("span", "chip", code)));
        usedBox.appendChild(chips);
      }
      usedBox.appendChild(
        technicalDetails([
          ["decision_id", decision.id],
          ["decision_type", decision.decision_type],
          ["reason_codes", (decision.reason_codes || []).join(", ")],
          ["model_version", decision.model_version],
          ["identity_kind", history && history.history ? history.history.identity_kind : null],
          ["identity_fingerprint", history && history.history ? history.history.identity_fingerprint : null],
          ["lookback_days", history && history.history ? history.history.lookback_days : null],
        ]),
      );
      usedBox.appendChild(
        el("p", "sub-note", "The payer's email is never copied into the decision record. Only an opaque fingerprint and the counts are."),
      );
      resultStep.content.appendChild(usedBox);
    }

    const actions = el("div", "actions");
    actions.appendChild(
      primaryButton("Pay again as the same payer", () => {
        state = { stage: "start", email: state.email, rounds: (state.rounds || 0) + 1 };
        rerender();
      }),
    );
    actions.appendChild(
      secondaryButton("Use a different payer", () => {
        state = { stage: "start", email: DEFAULT_EMAIL, rounds: 0 };
        rerender();
      }),
    );
    resultStep.content.appendChild(actions);
    container.appendChild(resultStep.node);
  }

  async function createOrder(email) {
    createStatus.set("Creating a real order in Razorpay Test Mode…");
    try {
      const result = await Api.createTestOrders(ctx.merchant.id, {
        kind: "customer_history",
        count: 1,
        amount: AMOUNT,
        label: "customer history",
      });
      const order = result.orders[0];
      state = {
        stage: "created",
        email,
        rounds: state.rounds || 0,
        experimentId: result.experiment_id,
        orderId: order.order_id,
        currency: order.currency,
      };
      createStatus.clear();
      rerender();
    } catch (error) {
      createStatus.clear();
      container.appendChild(notice(`The order could not be created: ${error.message}`, "bad"));
    }
  }

  async function pay() {
    try {
      await openCheckout({
        order: { order_id: state.orderId, amount: AMOUNT, currency: state.currency },
        prefill: { email: state.email },
        onClosed: () => {
          state.stage = "paid";
          rerender();
        },
      });
    } catch (error) {
      container.appendChild(notice(error.message, "bad"));
    }
  }

  async function run() {
    try {
      runStatus.set("Reading the payment from Razorpay and running the pipeline…");
      await Api.reconcile(ctx.merchant.id, state.orderId);

      runStatus.set("Reading back what was recorded…");
      const timeline = await Api.orderTimeline(state.orderId);

      let history = null;
      const attempt = timeline.payment_attempts.length
        ? timeline.payment_attempts[timeline.payment_attempts.length - 1]
        : null;
      if (attempt) {
        runStatus.set("Looking up this payer's previous payments…");
        history = await Api.customerHistory(attempt.id);
      }

      runStatus.clear();
      state.stage = "done";
      state.timeline = timeline;
      state.history = history;
      rerender();
    } catch (error) {
      runStatus.clear();
      container.appendChild(notice(`The pipeline could not complete: ${error.message}`, "bad"));
    }
  }

  rerender();
}
