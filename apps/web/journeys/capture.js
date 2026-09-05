"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { actionButton, createEventStream, createStageTrack, focusOn, pollWhile } from "../lib/live.js";
import { renderPipelineResult } from "../lib/pipeline.js";
import { PIPELINE_STAGES, applyTimeline, latestAttempt, outcomeHeadline } from "../lib/progress.js";
import { testDetailsLink } from "../lib/testcards.js";
import { clear, el, notice, rupees, secondaryButton } from "../lib/ui.js";

/**
 * Problem 01 -- an authorized payment needs a decision.
 *
 * The page is one continuous experiment rather than four boxes: a stage
 * track and an activity log stay on screen throughout, and the action area
 * beneath them changes to whatever the single next step is.
 *
 * Every stage lights up from a real snapshot. While the pipeline runs, the
 * timeline is polled -- reconciliation commits the observed payment before
 * any decision is made, so "payment observed" genuinely lands before
 * "recommendation", and the page shows that order because it happened, not
 * because it was scripted.
 */

const STORAGE_KEY = "journey.capture";

const AMOUNT_CHOICES = [
  {
    id: "within",
    amount: 50000,
    title: "₹500",
    caption: "Inside this merchant's automatic capture limit.",
    expectation: "Policy should allow the system to capture this on its own.",
  },
  {
    id: "above",
    amount: 800000,
    title: "₹8,000",
    caption: "Above that limit, inside the approval band.",
    expectation: "The system should still recommend capture — and policy should withhold it.",
  },
];

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
    /* resume-after-refresh is a convenience, not a requirement */
  }
}

export function renderCaptureJourney(container, ctx) {
  let state = load() || { stage: "choose" };
  const seenEvents = new Set();

  const track = createStageTrack(PIPELINE_STAGES);
  const stream = createEventStream();

  const stageArea = el("section", "experiment");
  const trackWrap = el("div", "experiment-track");
  trackWrap.appendChild(track.node);
  stageArea.appendChild(trackWrap);
  stageArea.appendChild(stream.node);

  const actionArea = el("section", "action-area");
  const resultArea = el("section", "result-area");

  // The next action leads. The stage track and activity log sit beneath
  // it as context: an evaluator arriving cold should see what to do first,
  // not six rows of "waiting".
  container.appendChild(actionArea);
  container.appendChild(stageArea);
  container.appendChild(resultArea);

  // ---- Restore whatever is already true, without re-announcing it ----
  if (state.orderId) {
    track.set("order", "done", `${state.orderId} · ${rupees(state.amount)}`);
  }
  if (state.timeline) {
    applyTimeline(track, null, state.timeline, seenEvents);
  }

  function persist() {
    save(state);
  }

  // ---------------------------------------------------------------------
  // Action area -- exactly one obvious next step at a time
  // ---------------------------------------------------------------------

  function renderAction() {
    clear(actionArea);

    if (state.stage === "choose") {
      actionArea.appendChild(el("h2", "action-title", "Create a real test order"));
      actionArea.appendChild(
        el("p", "action-lede", "Pick an amount. This merchant allows automatic capture up to a limit, so the amount decides what the system will be permitted to do."),
      );
      const choices = el("div", "choices");
      AMOUNT_CHOICES.forEach((choice) => {
        const card = el("button", "choice");
        card.type = "button";
        card.appendChild(el("span", "choice-amount", choice.title));
        card.appendChild(el("span", "choice-caption", choice.caption));
        card.appendChild(el("span", "choice-expectation", choice.expectation));
        card.addEventListener("click", () => createOrder(choice, card));
        choices.appendChild(card);
      });
      actionArea.appendChild(choices);
      return;
    }

    if (state.stage === "created") {
      actionArea.appendChild(el("h2", "action-title", "Your order is ready. Now pay it."));
      actionArea.appendChild(
        el("p", "action-lede", "This opens Razorpay's own Checkout. The payment details go to Razorpay, never to this site."),
      );
      const row = el("div", "action-row");
      row.appendChild(
        actionButton({
          label: "Pay with Razorpay",
          workingLabel: "Opening Razorpay…",
          onClick: pay,
        }),
      );
      row.appendChild(testDetailsLink());
      actionArea.appendChild(row);
      return;
    }

    if (state.stage === "paid") {
      actionArea.appendChild(el("h2", "action-title", "Checkout closed. Now find out what actually happened."));
      actionArea.appendChild(
        el("p", "action-lede", "The system will read this payment from Razorpay, decide what it recommends, check merchant policy, act only if allowed, and then verify the result independently."),
      );
      actionArea.appendChild(
        actionButton({
          label: "Let the system decide",
          workingLabel: "Working…",
          onClick: runPipeline,
        }),
      );
      return;
    }

    // done
    actionArea.appendChild(
      secondaryButton("Run it again with a different amount", () => {
        state = { stage: "choose" };
        seenEvents.clear();
        stream.clear();
        track.reset();
        track.set("order", "waiting", "");
        persist();
        clear(resultArea);
        renderAction();
        focusOn(actionArea);
      }),
    );
  }

  // ---------------------------------------------------------------------
  // Steps
  // ---------------------------------------------------------------------

  async function createOrder(choice, cardNode) {
    cardNode.classList.add("is-working");
    track.set("order", "working", "Asking Razorpay to create an order");
    stream.push(`Creating a ${choice.title} test order in Razorpay Test Mode…`);

    try {
      const result = await Api.createTestOrders(ctx.merchant.id, {
        kind: "capture_decision",
        count: 1,
        amount: choice.amount,
        label: `capture ${choice.id}`,
      });
      const order = result.orders[0];
      state = {
        stage: "created",
        experimentId: result.experiment_id,
        orderId: order.order_id,
        amount: order.amount,
        currency: order.currency,
      };
      persist();

      track.set("order", "done", `${order.order_id} · ${rupees(order.amount)}`);
      stream.push(`Order ${order.order_id} created for ${rupees(order.amount)}.`, "good");
      renderAction();
      focusOn(actionArea);
    } catch (error) {
      cardNode.classList.remove("is-working");
      track.set("order", "blocked", "Could not be created");
      stream.push(`Order creation failed: ${error.message}`, "bad");
      showError("The test order could not be created.", error, () => renderAction());
    }
  }

  async function pay() {
    stream.push("Opening Razorpay Checkout…");
    try {
      await openCheckout({
        order: { order_id: state.orderId, amount: state.amount, currency: state.currency },
        onClosed: (outcome) => {
          // Checkout's own result is only a signal to go and ask the
          // server. It is deliberately not shown as a payment state.
          stream.push(
            outcome.kind === "dismissed"
              ? "Checkout was closed without completing a payment."
              : "Checkout closed. Razorpay has not been asked yet.",
          );
          state.stage = "paid";
          persist();
          renderAction();
          focusOn(actionArea);
        },
      });
    } catch (error) {
      stream.push(error.message, "bad");
      showError("Razorpay Checkout could not be opened.", error, () => renderAction());
    }
  }

  async function runPipeline() {
    track.set("payment", "working", "Reading this payment from Razorpay");
    stream.push("Reading the payment from Razorpay…");

    try {
      // Poll the real timeline while the pipeline runs. Reconciliation
      // commits the observed payment before any decision exists, so these
      // stages arrive in the order they actually happen.
      const reconcile = Api.reconcile(ctx.merchant.id, state.orderId);
      await pollWhile(reconcile, {
        poll: () => Api.orderTimeline(state.orderId),
        onUpdate: (snapshot) => applyTimeline(track, stream, snapshot, seenEvents),
      });

      const timeline = await Api.orderTimeline(state.orderId);
      applyTimeline(track, stream, timeline, seenEvents);

      state.stage = "done";
      state.timeline = timeline;
      persist();

      renderResult(timeline);
      renderAction();
      focusOn(resultArea);
    } catch (error) {
      track.set("payment", "blocked", "Could not be read");
      stream.push(`The system could not complete: ${error.message}`, "bad");
      showError(
        error.status === 502
          ? "Razorpay could not be read for this order. Nothing was decided and no money moved."
          : "The system could not finish looking at this payment.",
        error,
        () => renderAction(),
      );
    }
  }

  // ---------------------------------------------------------------------
  // Result
  // ---------------------------------------------------------------------

  function renderResult(timeline) {
    clear(resultArea);

    const headline = outcomeHeadline(timeline);
    const banner = el("div", `outcome outcome-${headline.tone}`);
    banner.appendChild(el("span", "outcome-label", "Result"));
    banner.appendChild(el("h2", "outcome-title", headline.title));
    if (headline.amount !== undefined && headline.amount !== null) {
      banner.appendChild(el("div", "outcome-amount", `${rupees(headline.amount)} confirmed captured`));
    }
    if (headline.detail) banner.appendChild(el("p", "outcome-detail", headline.detail));
    resultArea.appendChild(banner);

    const attempt = latestAttempt(timeline);
    if (attempt && attempt.status === "captured" && timeline.decision && timeline.decision.decision_type === "NO_ACTION") {
      resultArea.appendChild(
        notice(
          "Razorpay had already captured this payment before the system looked at it, so there was no decision left to make. Create a new order to see the capture path.",
          "neutral",
        ),
      );
    }

    resultArea.appendChild(renderPipelineResult(timeline));
  }

  function showError(message, error, retry) {
    clear(resultArea);
    const box = el("div", "outcome outcome-blocked");
    box.appendChild(el("span", "outcome-label", "Problem"));
    box.appendChild(el("h2", "outcome-title", message));
    const details = el("details", "tech");
    details.appendChild(el("summary", null, "Technical detail"));
    details.appendChild(el("p", "tech-value", error && error.message ? error.message : String(error)));
    box.appendChild(details);
    resultArea.appendChild(box);
    if (retry) retry();
    focusOn(resultArea);
  }

  // ---------------------------------------------------------------------

  renderAction();
  if (state.stage === "done" && state.timeline) renderResult(state.timeline);
}
