"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { actionButton, createEventStream, createStageTrack, focusOn, pollWhile } from "../lib/live.js";
import { PIPELINE_STAGES, applyTimeline, decisionWord, latestAttempt } from "../lib/progress.js";
import { testDetailsLink } from "../lib/testcards.js";
import { clear, el, notice, rupees, secondaryButton, technicalDetails } from "../lib/ui.js";

/**
 * Problem 04 -- does the customer's previous payment behaviour change the
 * decision?
 *
 * The history is real, so the only way to build one is the real way: pay
 * more than once as the same payer. The page keeps count of how many runs
 * this browser has done, and the history panel fills in from the database
 * rather than from that counter.
 *
 * If the history does not change the recommendation, the page says exactly
 * that. Manufacturing a different decision to make the demo land would
 * defeat the point of showing the decision at all.
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
  let state = load() || { stage: "start", email: DEFAULT_EMAIL, runs: 0 };
  const seenEvents = new Set();

  const track = createStageTrack(PIPELINE_STAGES);
  const stream = createEventStream();

  const actionArea = el("section", "action-area");
  const contextArea = el("section", "result-area");
  const experiment = el("section", "experiment");
  const trackWrap = el("div", "experiment-track");
  trackWrap.appendChild(track.node);
  experiment.appendChild(trackWrap);
  experiment.appendChild(stream.node);

  container.appendChild(actionArea);
  container.appendChild(experiment);
  container.appendChild(contextArea);

  if (state.orderId) track.set("order", "done", `${state.orderId} · ${rupees(AMOUNT)}`);
  if (state.timeline) applyTimeline(track, null, state.timeline, seenEvents);

  function persist() {
    save(state);
  }

  // ---------------------------------------------------------------------

  function renderAction() {
    clear(actionArea);

    if (state.stage === "start") {
      actionArea.appendChild(el("h2", "action-title", "Choose the customer"));
      actionArea.appendChild(
        el("p", "action-lede", "History is matched on the email Razorpay records with the payment. Use the same address each time and this payer builds a real history you can watch the system read."),
      );

      const field = el("label", "field");
      field.appendChild(el("span", "field-label", "Payer email"));
      const input = el("input", "field-input");
      input.type = "email";
      input.value = state.email;
      field.appendChild(input);
      actionArea.appendChild(field);

      actionArea.appendChild(
        actionButton({
          label: "Create a test order for this payer",
          workingLabel: "Creating order…",
          onClick: () => createOrder(input.value.trim() || DEFAULT_EMAIL),
        }),
      );

      if (state.runs > 0) {
        actionArea.appendChild(
          notice(`You have completed ${state.runs} payment${state.runs === 1 ? "" : "s"} as this payer from this browser. Each one adds to the history the system reads.`, "neutral"),
        );
      }
      return;
    }

    if (state.stage === "created") {
      actionArea.appendChild(el("h2", "action-title", "Pay as that customer"));
      actionArea.appendChild(
        el("p", "action-lede", "Checkout opens with that email prefilled. Succeed or fail it deliberately — both are useful history."),
      );
      const row = el("div", "action-row");
      row.appendChild(actionButton({ label: "Pay with Razorpay", workingLabel: "Opening Razorpay…", onClick: pay }));
      row.appendChild(testDetailsLink());
      actionArea.appendChild(row);
      return;
    }

    if (state.stage === "paid") {
      actionArea.appendChild(el("h2", "action-title", "Let the system look at this payment and the payer's past"));
      actionArea.appendChild(
        actionButton({ label: "Check payment history", workingLabel: "Working…", onClick: run }),
      );
      return;
    }

    const actions = el("div", "actions");
    actions.appendChild(
      actionButton({
        label: "Pay again as the same payer",
        onClick: async () => {
          state = { stage: "start", email: state.email, runs: (state.runs || 0) + 1 };
          seenEvents.clear();
          stream.clear();
          track.reset();
          persist();
          clear(contextArea);
          renderAction();
          focusOn(actionArea);
        },
      }),
    );
    actions.appendChild(
      secondaryButton("Use a different payer", () => {
        state = { stage: "start", email: DEFAULT_EMAIL, runs: 0 };
        seenEvents.clear();
        stream.clear();
        track.reset();
        persist();
        clear(contextArea);
        renderAction();
        focusOn(actionArea);
      }),
    );
    actionArea.appendChild(actions);
  }

  // ---------------------------------------------------------------------

  async function createOrder(email) {
    track.set("order", "working", "Asking Razorpay to create an order");
    stream.push(`Creating a test order for ${email}…`);
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
        runs: state.runs || 0,
        experimentId: result.experiment_id,
        orderId: order.order_id,
        currency: order.currency,
      };
      persist();
      track.set("order", "done", `${order.order_id} · ${rupees(AMOUNT)}`);
      stream.push(`Order ${order.order_id} created.`, "good");
      renderAction();
      focusOn(actionArea);
    } catch (error) {
      track.set("order", "blocked", "Could not be created");
      stream.push(`Order creation failed: ${error.message}`, "bad");
      clear(contextArea);
      contextArea.appendChild(notice(`The test order could not be created: ${error.message}`, "bad"));
    }
  }

  async function pay() {
    try {
      await openCheckout({
        order: { order_id: state.orderId, amount: AMOUNT, currency: state.currency },
        prefill: { email: state.email },
        onClosed: () => {
          stream.push("Checkout closed. Razorpay has not been asked yet.");
          state.stage = "paid";
          persist();
          renderAction();
          focusOn(actionArea);
        },
      });
    } catch (error) {
      stream.push(error.message, "bad");
    }
  }

  async function run() {
    track.set("payment", "working", "Reading this payment from Razorpay");
    stream.push("Reading the payment from Razorpay…");
    try {
      const reconcile = Api.reconcile(ctx.merchant.id, state.orderId);
      await pollWhile(reconcile, {
        poll: () => Api.orderTimeline(state.orderId),
        onUpdate: (snapshot) => applyTimeline(track, stream, snapshot, seenEvents),
      });

      const timeline = await Api.orderTimeline(state.orderId);
      applyTimeline(track, stream, timeline, seenEvents);

      let history = null;
      const attempt = latestAttempt(timeline);
      if (attempt) {
        stream.push("Looking up this payer's previous payments…");
        history = await Api.customerHistory(attempt.id);
        if (history.identity_available && history.history) {
          stream.push(
            `Found ${history.history.prior_payment_count} previous payment${history.history.prior_payment_count === 1 ? "" : "s"} for this payer.`,
          );
        } else {
          stream.push("This payment carries nothing to recognise a payer by.");
        }
      }

      state.stage = "done";
      state.timeline = timeline;
      state.history = history;
      persist();

      renderContext();
      renderAction();
      focusOn(contextArea);
    } catch (error) {
      track.set("payment", "blocked", "Could not be read");
      stream.push(`The system could not complete: ${error.message}`, "bad");
      clear(contextArea);
      contextArea.appendChild(notice(`The pipeline could not complete: ${error.message}`, "bad"));
    }
  }

  // ---------------------------------------------------------------------

  function renderContext() {
    clear(contextArea);
    const timeline = state.timeline;
    const history = state.history;
    const attempt = latestAttempt(timeline);

    contextArea.appendChild(el("h2", "action-title", "What the system knew when it decided"));

    // ---- The equation: current + history = context ----
    const equation = el("div", "equation");

    const current = el("div", "equation-part");
    current.appendChild(el("span", "equation-label", "This payment"));
    if (attempt) {
      current.appendChild(el("span", "equation-value", rupees(attempt.amount)));
      current.appendChild(el("span", `equation-tag equation-${attempt.status === "failed" ? "bad" : "good"}`, attempt.status));
    } else {
      current.appendChild(el("span", "equation-value", "—"));
      current.appendChild(el("span", "equation-tag", "not observed"));
    }
    equation.appendChild(current);

    equation.appendChild(el("div", "equation-op", "+"));

    const past = el("div", "equation-part");
    past.appendChild(el("span", "equation-label", "What came before"));
    if (!history || !history.identity_available) {
      past.appendChild(el("span", "equation-value", "No identity"));
      past.appendChild(el("span", "equation-tag", "cannot look up"));
    } else if (history.history.prior_payment_count === 0) {
      past.appendChild(el("span", "equation-value", "None yet"));
      past.appendChild(el("span", "equation-tag", "first payment"));
    } else {
      const strip = el("div", "history-strip");
      for (let i = 0; i < history.history.prior_captured_count; i += 1) {
        strip.appendChild(el("span", "history-chip history-good", "✓"));
      }
      for (let i = 0; i < history.history.prior_failed_count; i += 1) {
        strip.appendChild(el("span", "history-chip history-bad", "✕"));
      }
      strip.appendChild(el("span", "history-chip history-current", "now"));
      past.appendChild(strip);
      past.appendChild(
        el("span", "equation-tag", `${history.history.prior_captured_count} succeeded · ${history.history.prior_failed_count} failed`),
      );
    }
    equation.appendChild(past);

    equation.appendChild(el("div", "equation-op", "="));

    const result = el("div", "equation-part equation-result");
    result.appendChild(el("span", "equation-label", "Decision context"));
    result.appendChild(
      el("span", "equation-value", timeline.decision ? decisionWord(timeline.decision.decision_type) : "No decision"),
    );
    equation.appendChild(result);
    contextArea.appendChild(equation);

    // ---- Was the history actually used? ----
    if (timeline.decision) {
      const codes = (timeline.decision.reason_codes || []).filter(
        (code) => code.startsWith("CUSTOMER_HISTORY") || code.startsWith("PRIOR_CUSTOMER"),
      );
      const verdict = el("div", "verdict");
      if (codes.length) {
        verdict.appendChild(el("h3", "sub-title", "History changed what the system did"));
        verdict.appendChild(
          el("p", "story-line", "The payer's record is part of the recorded reason for this decision:"),
        );
        const chips = el("div", "chips");
        codes.forEach((code) => chips.appendChild(el("span", "chip", code)));
        verdict.appendChild(chips);
      } else {
        verdict.appendChild(el("h3", "sub-title", "History was considered, and did not change the recommendation"));
        verdict.appendChild(
          el(
            "p",
            "story-line",
            history && history.identity_available && history.history.prior_payment_count > 0
              ? `The system read ${history.history.prior_payment_count} previous payment${history.history.prior_payment_count === 1 ? "" : "s"} for this payer and still recommended the same thing. That is a real result, not a missing feature — the history is in the decision's context either way.`
              : "There was no prior history to weigh yet. Pay again as the same payer to build one.",
          ),
        );
      }
      verdict.appendChild(
        technicalDetails([
          ["decision_id", timeline.decision.id],
          ["decision_type", timeline.decision.decision_type],
          ["reason_codes", (timeline.decision.reason_codes || []).join(", ")],
          ["model_version", timeline.decision.model_version],
          ["identity_kind", history && history.history ? history.history.identity_kind : null],
          ["identity_fingerprint", history && history.history ? history.history.identity_fingerprint : null],
          ["lookback_days", history && history.history ? history.history.lookback_days : null],
          ["prior_payment_count", history && history.history ? history.history.prior_payment_count : null],
        ]),
      );
      verdict.appendChild(
        el("p", "sub-note", "The payer's email never reaches the decision record — only an opaque fingerprint and the counts."),
      );
      contextArea.appendChild(verdict);
    }
  }

  renderAction();
  if (state.stage === "done" && state.timeline) renderContext();
}
