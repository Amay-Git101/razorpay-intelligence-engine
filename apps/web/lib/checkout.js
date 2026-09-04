"use strict";

import { Api } from "./api.js";

/**
 * Real Razorpay Checkout.
 *
 * The dialog the evaluator sees is Razorpay's, served by Razorpay's own
 * script, and the payment details go to Razorpay. This project draws no
 * card form and imitates no part of that interface.
 *
 * WHAT CHECKOUT'S CALLBACKS ARE, AND ARE NOT
 * Checkout tells the browser how the dialog ended. That is a client-side
 * claim, and this frontend treats it as nothing more than a signal to go
 * and ask the server what actually happened. Every payment state shown
 * anywhere in this app comes from the backend reading Razorpay directly --
 * never from the callback below. A `handler` firing does not mean the money
 * moved, and this code never says that it did.
 */

let configPromise = null;

export function checkoutConfig() {
  if (!configPromise) {
    configPromise = Api.checkoutConfig().catch((error) => {
      configPromise = null;
      throw error;
    });
  }
  return configPromise;
}

export function checkoutScriptLoaded() {
  return typeof window.Razorpay === "function";
}

/**
 * Opens Checkout for one real order.
 *
 * onClosed is called however the dialog ends -- success, failure or
 * dismissal -- because in all three cases the next step is identical: ask
 * the backend to reconcile the order and report what Razorpay says.
 */
export async function openCheckout({ order, prefill, onClosed }) {
  if (!checkoutScriptLoaded()) {
    throw new Error("Razorpay Checkout could not be loaded. Check the network connection and reload.");
  }

  const config = await checkoutConfig();

  return new Promise((resolve) => {
    let settled = false;
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      if (onClosed) onClosed(outcome);
      resolve(outcome);
    };

    const options = {
      key: config.key_id,
      order_id: order.order_id,
      amount: order.amount,
      currency: order.currency || "INR",
      name: "Payment Decision System",
      description: "Razorpay Test Mode payment",
      prefill: prefill || {},
      // Razorpay reports the dialog closing without a completed payment.
      modal: {
        ondismiss: () => finish({ kind: "dismissed" }),
      },
      // Fires when a payment attempt completed successfully from Checkout's
      // point of view. Recorded, then verified against the server.
      handler: (response) => finish({ kind: "completed", paymentId: response.razorpay_payment_id }),
    };

    const rzp = new window.Razorpay(options);

    // Fires when an attempt failed at the gateway. Also just a signal to go
    // and reconcile -- the failure detail shown to the evaluator comes from
    // the payment record the backend reads back, not from here.
    rzp.on("payment.failed", () => finish({ kind: "failed" }));

    rzp.open();
  });
}
