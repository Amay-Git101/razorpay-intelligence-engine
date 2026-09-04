"use strict";

/**
 * Every backend call the frontend makes, in one place.
 *
 * All paths are relative, so the page talks to whatever origin served it.
 * There is no Razorpay REST call here and no database access here: the only
 * external thing this frontend touches directly is Razorpay's own Checkout
 * script, which needs the publishable key and nothing else.
 */

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (networkError) {
    throw new Error("Could not reach the server.");
  }

  let body = null;
  try {
    body = await response.json();
  } catch (_) {
    // Some responses legitimately have no JSON body; handled below.
  }

  if (!response.ok) {
    const detail = body && body.detail ? body.detail : `Request failed (HTTP ${response.status}).`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return body;
}

function postJson(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const Api = {
  health: () => request("/health"),

  // The publishable key id only. There is no endpoint that returns a secret.
  checkoutConfig: () => request("/checkout-config"),

  merchants: () => request("/merchants"),

  createTestOrders: (merchantId, payload) =>
    postJson(`/merchants/${encodeURIComponent(merchantId)}/test-orders`, payload),

  experiment: (experimentId) => request(`/experiments/${encodeURIComponent(experimentId)}`),

  experimentFailurePattern: (experimentId) =>
    request(`/experiments/${encodeURIComponent(experimentId)}/failure-pattern`),

  merchantFailurePattern: (merchantId, limit = 20) =>
    request(`/merchants/${encodeURIComponent(merchantId)}/failure-pattern?limit=${limit}`),

  // Runs the real pipeline: reconcile -> decide -> policy -> action -> verify.
  reconcile: (merchantId, orderId) =>
    request(
      `/merchants/${encodeURIComponent(merchantId)}/orders/${encodeURIComponent(orderId)}/reconcile`,
      { method: "POST" },
    ),

  orderTimeline: (orderId) => request(`/orders/${encodeURIComponent(orderId)}/timeline`),

  customerHistory: (paymentAttemptId) =>
    request(`/payments/${encodeURIComponent(paymentAttemptId)}/customer-history`),
};
