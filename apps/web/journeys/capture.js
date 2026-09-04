"use strict";

import { Api } from "../lib/api.js";
import { openCheckout } from "../lib/checkout.js";
import { renderPipelineResult } from "../lib/pipeline.js";
import {
  clear,
  el,
  notice,
  primaryButton,
  rupees,
  secondaryButton,
  statusLine,
  step,
} from "../lib/ui.js";

/**
 * Problem 01 -- an authorized payment needs a decision.
 *
 * The evaluator picks an amount, pays a real Razorpay Test Mode order, and
 * the real pipeline decides what happens to it. The amount is the whole
 * experiment: this merchant's policy allows automatic capture up to a
 * limit, so a small payment and a large one take genuinely different paths
 * through the same code.
 *
 * Nothing here decides anything. Every state rendered comes from the
 * backend after it read Razorpay.
 */

const STORAGE_KEY = "journey.capture";

// Two amounts chosen to sit on either side of this merchant's configured
// automatic-capture limit. The captions describe what is expected; the
// backend's policy engine is what actually decides, and the outcome shown
// is read from its decision. If the merchant's configuration changed, the
// result displayed would change with it -- nothing here is asserting the
// answer in advance.
const AMOUNT_CHOICES = [
  {
    id: "within",
    amount: 50000,
    title: "₹500",
    caption: "Within this merchant's automatic capture limit.",
    expectation: "Expected: the system recommends capture and policy allows it.",
  },
  {
    id: "above",
    amount: 800000,
    title: "₹8,000",
    caption: "Above the automatic limit, inside the approval band.",
    expectation: "Expected: the system still recommends capture, but policy withholds automatic authority.",
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
    // A browser refusing storage only costs resume-after-refresh.
  }
}

function reset() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (_) {
    /* ignore */
  }
}

export function renderCaptureJourney(container, ctx) {
  let state = load() || { stage: "choose" };

  function rerender() {
    save(state);
    clear(container);
    draw();
  }

  function draw() {
    // ---- Step 1: create a real order ----
    const chooseStep = step({
      number: 1,
      title: "Create a test order",
      state: state.stage === "choose" ? "active" : "done",
    });

    if (state.stage === "choose") {
      chooseStep.content.appendChild(
        el("p", "lede", "Pick an amount. The merchant's policy allows automatic capture up to a limit, so this choice changes what the system is allowed to do later."),
      );
      const choices = el("div", "choices");
      AMOUNT_CHOICES.forEach((choice) => {
        const card = el("button", "choice");
        card.type = "button";
        card.appendChild(el("span", "choice-amount", choice.title));
        card.appendChild(el("span", "choice-caption", choice.caption));
        card.appendChild(el("span", "choice-expectation", choice.expectation));
        card.addEventListener("click", () => createOrder(choice));
        choices.appendChild(card);
      });
      chooseStep.content.appendChild(choices);
      chooseStep.content.appendChild(status.node);
    } else {
      chooseStep.content.appendChild(
        el("p", "done-line", `Order ${state.orderId} created for ${rupees(state.amount)} in Razorpay Test Mode.`),
      );
    }
    container.appendChild(chooseStep.node);

    if (state.stage === "choose") return;

    // ---- Step 2: pay it, for real ----
    const payStep = step({
      number: 2,
      title: "Complete the payment",
      state: state.stage === "created" ? "active" : "done",
    });

    if (state.stage === "created") {
      payStep.content.appendChild(
        el("p", "lede", "This opens Razorpay's own Checkout. Use any Razorpay Test Mode payment method — the details go to Razorpay, not to this site."),
      );
      payStep.content.appendChild(primaryButton("Pay with Razorpay", pay));
      payStep.content.appendChild(payStatus.node);
    } else {
      payStep.content.appendChild(el("p", "done-line", "Checkout closed. The result below came from asking Razorpay, not from Checkout."));
    }
    container.appendChild(payStep.node);

    if (state.stage === "created") return;

    // ---- Step 3: run the real pipeline ----
    const checkStep = step({
      number: 3,
      title: "Let the system decide",
      state: state.stage === "paid" ? "active" : "done",
    });

    if (state.stage === "paid") {
      checkStep.content.appendChild(
        el("p", "lede", "The system will read this payment from Razorpay, work out what it is, decide what it recommends, check merchant policy, act only if allowed, and then independently verify what happened."),
      );
      checkStep.content.appendChild(primaryButton("Run the system", runPipeline));
      checkStep.content.appendChild(runStatus.node);
    } else {
      checkStep.content.appendChild(el("p", "done-line", "The pipeline ran against the live payment."));
    }
    container.appendChild(checkStep.node);

    if (state.stage !== "done") return;

    // ---- Step 4: what actually happened ----
    const resultStep = step({ number: 4, title: "What actually happened", state: "active" });
    if (state.timeline) {
      resultStep.content.appendChild(renderPipelineResult(state.timeline));
    }
    const again = el("div", "actions");
    again.appendChild(
      secondaryButton("Run it again with a different amount", () => {
        reset();
        state = { stage: "choose" };
        rerender();
      }),
    );
    resultStep.content.appendChild(again);
    container.appendChild(resultStep.node);
  }

  const status = statusLine();
  const payStatus = statusLine();
  const runStatus = statusLine();

  async function createOrder(choice) {
    status.set("Creating a real order in Razorpay Test Mode…");
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
      rerender();
    } catch (error) {
      status.clear();
      container.appendChild(notice(`The order could not be created: ${error.message}`, "bad"));
    }
  }

  async function pay() {
    payStatus.set("Opening Razorpay Checkout…");
    try {
      await openCheckout({
        order: { order_id: state.orderId, amount: state.amount, currency: state.currency },
        onClosed: () => {
          // However Checkout ended, the next step is the same: ask the
          // server. Checkout's own callbacks are not treated as truth.
          payStatus.clear();
          state.stage = "paid";
          rerender();
        },
      });
    } catch (error) {
      payStatus.clear();
      container.appendChild(notice(error.message, "bad"));
    }
  }

  async function runPipeline() {
    try {
      runStatus.set("Reading the payment from Razorpay and running the pipeline…");
      await Api.reconcile(ctx.merchant.id, state.orderId);

      runStatus.set("Reading back what was recorded…");
      const timeline = await Api.orderTimeline(state.orderId);

      runStatus.clear();
      state.stage = "done";
      state.timeline = timeline;
      rerender();
    } catch (error) {
      runStatus.clear();
      const message =
        error.status === 502
          ? "Razorpay could not be read for this order. Nothing was decided and nothing was captured."
          : `The pipeline could not complete: ${error.message}`;
      container.appendChild(notice(message, "bad"));
    }
  }

  rerender();
}
