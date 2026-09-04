"use strict";

import { Api } from "../lib/api.js";
import { renderFailurePattern } from "../lib/pattern.js";
import { clear, el, notice, primaryButton, statusLine, step } from "../lib/ui.js";

/**
 * Problem 02 -- is the payment gateway having trouble?
 *
 * Looks across this merchant's most recent payments rather than a fixed
 * group, which is the difference from problem 03: this is the question a
 * merchant asks about live traffic, with a window that moves.
 *
 * The honest limit is stated in the report itself and repeated here: this
 * system can see one merchant's payments in its own database. It cannot see
 * Razorpay's platform health. So the strongest thing it will ever say is
 * that failures are concentrated, which is consistent with a wider problem
 * without establishing one.
 */

const WINDOW_SIZE = 20;

export function renderGatewayJourney(container, ctx) {
  let report = null;
  const status = statusLine();

  function rerender() {
    clear(container);
    draw();
  }

  function draw() {
    const checkStep = step({
      number: 1,
      title: "Look at what this merchant's recent payments actually did",
      state: "active",
    });
    checkStep.content.appendChild(
      el("p", "lede", `This counts the last ${WINDOW_SIZE} payments recorded for ${ctx.merchant.name} and reports what it finds — the counts first, the arithmetic second, and only then what it might mean.`),
    );
    checkStep.content.appendChild(primaryButton(report ? "Check again" : "Check recent payments", check));
    checkStep.content.appendChild(status.node);
    container.appendChild(checkStep.node);

    if (!report) return;

    const resultStep = step({ number: 2, title: "What the numbers show", state: "active" });
    resultStep.content.appendChild(renderFailurePattern(report));

    if (report.interpretation.code === "INSUFFICIENT_DATA") {
      resultStep.content.appendChild(
        notice(
          "There are not enough observed payments to say anything yet. Problem 03 creates a group of six payments you can drive yourself, which is the fastest way to give this something to read.",
          "neutral",
        ),
      );
      const link = el("a", "btn btn-secondary", "Go to problem 03");
      link.href = "#/problem/3";
      resultStep.content.appendChild(link);
    }

    container.appendChild(resultStep.node);
  }

  async function check() {
    status.set("Counting recent payments…");
    try {
      report = await Api.merchantFailurePattern(ctx.merchant.id, WINDOW_SIZE);
      status.clear();
      rerender();
    } catch (error) {
      status.clear();
      container.appendChild(notice(`Could not read recent payments: ${error.message}`, "bad"));
    }
  }

  rerender();
}
