"use strict";

import { Api } from "../lib/api.js";
import { actionButton, createEventStream, focusOn } from "../lib/live.js";
import { renderFailurePattern } from "../lib/pattern.js";
import { clear, el, notice, percent } from "../lib/ui.js";

/**
 * Problem 02 -- is the payment gateway having trouble?
 *
 * An investigation rather than a statistics page: ask the question, look
 * at what the payments actually did, then read the answer in the order a
 * claim should be read.
 *
 * The dots are one per real observed payment. They fade in with a short
 * stagger, which is an entrance animation over data that has already
 * arrived -- not a pretence that they are streaming in one at a time.
 */

const WINDOW_SIZE = 20;

function dotTone(report, index) {
  // The report gives counts, not an ordered list, so the strip is drawn
  // from the counts it does give: failures first, then everything else.
  // It represents the composition of the window, and says so.
  if (index < report.observed.failed) return "bad";
  if (index < report.observed.failed + report.observed.captured) return "good";
  return "neutral";
}

export function renderGatewayJourney(container, ctx) {
  let report = null;
  const stream = createEventStream();

  const askArea = el("section", "action-area");
  const resultArea = el("section", "result-area");
  container.appendChild(askArea);
  container.appendChild(stream.node);
  container.appendChild(resultArea);

  function renderAsk() {
    clear(askArea);
    askArea.appendChild(el("h2", "action-title", "Look at what the payments actually did"));
    askArea.appendChild(
      el("p", "action-lede", `This reads the last ${WINDOW_SIZE} payments recorded for ${ctx.merchant.name} — the counts first, the arithmetic second, and only then what it might mean.`),
    );
    askArea.appendChild(
      actionButton({
        label: report ? "Check again" : "Check recent payments",
        workingLabel: "Reading recent payment activity…",
        onClick: check,
      }),
    );
  }

  async function check() {
    stream.push("Reading recent payment activity…");
    try {
      report = await Api.merchantFailurePattern(ctx.merchant.id, WINDOW_SIZE);
      stream.push(
        `${report.observed.payments_observed} payments read — ${report.observed.failed} failed.`,
        report.observed.failed ? "bad" : "good",
      );
      if (report.computed.failure_rate !== null && report.computed.failure_rate !== undefined) {
        stream.push(`Failure rate: ${percent(report.computed.failure_rate)}.`);
      }
      stream.push(`Conclusion: ${report.interpretation.headline}`, report.interpretation.consistent_with_wider_problem ? "bad" : "good");
      renderAsk();
      renderResult();
      focusOn(resultArea);
    } catch (error) {
      stream.push(`Could not read recent payments: ${error.message}`, "bad");
      clear(resultArea);
      resultArea.appendChild(notice(`Recent payments could not be read: ${error.message}`, "bad"));
    }
  }

  function renderResult() {
    clear(resultArea);
    if (!report) return;

    // ---- The activity strip ----
    const strip = el("section", "signal");
    strip.appendChild(el("h3", "signal-title", "What the last payments were"));
    strip.appendChild(
      el("p", "signal-sub", "One mark per observed payment, grouped by outcome. The API returns counts rather than an ordered list, so this shows the composition of the window, not the sequence."),
    );
    const dots = el("div", "dots");
    dots.setAttribute(
      "aria-label",
      `${report.observed.payments_observed} payments observed, ${report.observed.failed} failed`,
    );
    for (let i = 0; i < report.observed.payments_observed; i += 1) {
      const dot = el("span", `dot dot-${dotTone(report, i)}`);
      dot.style.animationDelay = `${i * 40}ms`;
      dots.appendChild(dot);
    }
    strip.appendChild(dots);
    // Every dot is accounted for: a mark with no explanation invites the
    // reader to guess what it means.
    const parts = [`${report.observed.failed} failed`, `${report.observed.captured} captured`];
    if (report.observed.authorized_not_captured) {
      parts.push(`${report.observed.authorized_not_captured} authorized but not captured`);
    }
    if (report.observed.other_states) parts.push(`${report.observed.other_states} in another state`);
    strip.appendChild(
      el("p", "signal-legend", `${parts.join(" · ")} — ${report.observed.payments_observed} observed in total`),
    );
    resultArea.appendChild(strip);

    resultArea.appendChild(renderFailurePattern(report));

    if (report.interpretation.code === "INSUFFICIENT_DATA") {
      const hint = el("div", "next-hint");
      hint.appendChild(
        el("p", "action-lede", "There is not enough payment activity here to say anything yet. Problem 03 creates six payments you drive yourself, which is the quickest way to give this something to read."),
      );
      const link = el("a", "btn btn-secondary", "Go to problem 03");
      link.href = "#/problem/3";
      hint.appendChild(link);
      resultArea.appendChild(hint);
    }
  }

  renderAsk();
}
