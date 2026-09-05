"use strict";

import { el } from "./ui.js";

/**
 * Razorpay Test Mode payment details, as a reference the evaluator can
 * reach at the moment they need it.
 *
 * WHAT THIS IS NOT
 * It is not a payment form. Nothing here is typed into this page, sent to
 * this backend, or stored anywhere. The evaluator copies a number and
 * types it into Razorpay's own Checkout. This project never touches card
 * data, never injects into Razorpay's iframe, and never imitates its UI.
 *
 * SOURCE
 * Values below were read from Razorpay's official Test Mode documentation
 * (razorpay.com/docs/payments/payments/test-card-details/ and
 * .../test-upi-details/) rather than copied from an older screenshot,
 * because a stale card number wastes the evaluator's time at exactly the
 * wrong moment. They are public test credentials and carry no secret.
 */

export const TEST_CARDS = [
  { network: "Visa", number: "4100 2800 0000 1007", type: "Debit", subType: "Consumer" },
  { network: "Mastercard", number: "5555 5100 0008 1006", type: "Credit", subType: "Business" },
  { network: "Mastercard", number: "5180 2872 0009 1001", type: "Prepaid", subType: "Consumer" },
  { network: "RuPay", number: "6527 6589 0000 1005", type: "Credit", subType: "Consumer" },
  { network: "Diners", number: "3608 280009 1007", type: "Credit", subType: "Consumer" },
  { network: "Amex", number: "3402 560004 01007", type: "Credit", subType: "Consumer" },
];

export const TEST_UPI = [
  { id: "success@razorpay", outcome: "Payment succeeds" },
  { id: "failure@razorpay", outcome: "Payment fails" },
];

let drawerNode = null;
let lastFocused = null;

function copyButton(value) {
  const button = el("button", "copy-btn", "Copy");
  button.type = "button";
  button.setAttribute("aria-label", `Copy card number ${value}`);
  button.addEventListener("click", async () => {
    const plain = value.replace(/\s/g, "");
    try {
      await navigator.clipboard.writeText(plain);
      button.textContent = "Copied";
      button.classList.add("is-copied");
    } catch (_) {
      // Clipboard can be blocked; selecting the text is the fallback that
      // always works, and saying so beats a button that silently fails.
      button.textContent = "Select it";
    }
    setTimeout(() => {
      button.textContent = "Copy";
      button.classList.remove("is-copied");
    }, 1600);
  });
  return button;
}

function buildDrawer() {
  const overlay = el("div", "drawer-overlay");
  const panel = el("aside", "drawer");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "Razorpay test payment details");

  const head = el("header", "drawer-head");
  const titles = el("div");
  titles.appendChild(el("h2", "drawer-title", "Razorpay test payment details"));
  titles.appendChild(
    el("p", "drawer-sub", "Type these into Razorpay's Checkout. Test Mode only — no real money is charged."),
  );
  head.appendChild(titles);

  const close = el("button", "drawer-close", "✕");
  close.type = "button";
  close.setAttribute("aria-label", "Close test payment details");
  close.addEventListener("click", closeTestDetails);
  head.appendChild(close);
  panel.appendChild(head);

  const context = el("p", "drawer-context");
  context.id = "drawer-context";
  panel.appendChild(context);

  // ---- How to succeed / fail ----
  const how = el("section", "drawer-section");
  how.appendChild(el("h3", "drawer-section-title", "Getting the outcome you want"));

  const succeed = el("div", "how-row");
  succeed.appendChild(el("span", "how-tag how-good", "Succeed"));
  succeed.appendChild(
    el("p", "how-text", "Pick Card, enter a test card below with any random CVV and any future expiry, then submit the OTP screen normally."),
  );
  how.appendChild(succeed);

  const fail = el("div", "how-row");
  fail.appendChild(el("span", "how-tag how-bad", "Fail"));
  fail.appendChild(
    el("p", "how-text", "Same card, but on Razorpay's OTP screen enter an OTP shorter than 4 digits and submit. Razorpay then fails the payment."),
  );
  how.appendChild(fail);
  panel.appendChild(how);

  // ---- Cards ----
  const cards = el("section", "drawer-section");
  cards.appendChild(el("h3", "drawer-section-title", "Test cards"));
  cards.appendChild(el("p", "drawer-note", "Any random CVV. Any future expiry date."));

  const list = el("div", "card-list");
  TEST_CARDS.forEach((card) => {
    const row = el("div", "card-row");
    const left = el("div", "card-left");
    left.appendChild(el("span", "card-number", card.number));
    left.appendChild(el("span", "card-meta", `${card.network} · ${card.type} · ${card.subType}`));
    row.appendChild(left);
    row.appendChild(copyButton(card.number));
    list.appendChild(row);
  });
  cards.appendChild(list);
  panel.appendChild(cards);

  // ---- UPI ----
  const upi = el("section", "drawer-section");
  upi.appendChild(el("h3", "drawer-section-title", "Test UPI"));
  const upiList = el("div", "card-list");
  TEST_UPI.forEach((entry) => {
    const row = el("div", "card-row");
    const left = el("div", "card-left");
    left.appendChild(el("span", "card-number", entry.id));
    left.appendChild(el("span", "card-meta", entry.outcome));
    row.appendChild(left);
    row.appendChild(copyButton(entry.id));
    upiList.appendChild(row);
  });
  upi.appendChild(upiList);
  upi.appendChild(
    el("p", "drawer-note", "Razorpay notes that in Test Mode a cancelled UPI payment still comes back successful, so use the failure id above to produce a failure."),
  );
  panel.appendChild(upi);

  panel.appendChild(
    el("p", "drawer-footnote", "Public Razorpay Test Mode credentials. Nothing entered in Razorpay's Checkout passes through this site."),
  );

  overlay.appendChild(panel);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeTestDetails();
  });
  return overlay;
}

function onKeydown(event) {
  if (event.key === "Escape") closeTestDetails();
}

/**
 * @param {string} [contextLabel] e.g. "Payment 3" -- names which payment
 *   the evaluator is about to make. The card details themselves are the
 *   same for every payment; only the label changes, because inventing
 *   per-payment card data would be fabrication.
 */
export function openTestDetails(contextLabel) {
  if (!drawerNode) {
    drawerNode = buildDrawer();
    document.body.appendChild(drawerNode);
  }
  const context = drawerNode.querySelector("#drawer-context");
  context.textContent = contextLabel
    ? `For ${contextLabel}. The same test details work for every payment.`
    : "";
  context.hidden = !contextLabel;

  lastFocused = document.activeElement;
  drawerNode.classList.add("is-open");
  document.addEventListener("keydown", onKeydown);
  const close = drawerNode.querySelector(".drawer-close");
  if (close) close.focus();
}

export function closeTestDetails() {
  if (!drawerNode) return;
  drawerNode.classList.remove("is-open");
  document.removeEventListener("keydown", onKeydown);
  if (lastFocused && lastFocused.focus) lastFocused.focus();
}

/** The small inline affordance placed next to a Pay button. */
export function testDetailsLink(contextLabel) {
  const button = el("button", "link-btn", "Need test card details?");
  button.type = "button";
  button.addEventListener("click", () => openTestDetails(contextLabel));
  return button;
}
