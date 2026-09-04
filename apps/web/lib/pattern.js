"use strict";

import { el, fact, percent, pill, technicalDetails } from "./ui.js";

/**
 * Renders a failure-pattern report with its three layers kept visually
 * apart, in the order a claim should be read: what was counted, what was
 * computed from it, and only then what it might mean.
 *
 * The layers are separate on the server too. Keeping them separate here is
 * the point -- an interpretation printed without the counts underneath it
 * is just an assertion.
 */

const TONE_BY_CODE = {
  CONCENTRATED_FAILURES: "bad",
  MULTIPLE_FAILURES: "attention",
  ISOLATED_FAILURE: "good",
  NO_FAILURES: "good",
  INSUFFICIENT_DATA: "neutral",
};

export function renderFailurePattern(report) {
  const wrap = el("div", "pattern");

  // ---- 1. Observed ----
  const observedSection = el("section", "layer layer-observed");
  const observedHead = el("div", "layer-head");
  observedHead.appendChild(el("span", "layer-tag", "Observed"));
  observedHead.appendChild(el("span", "layer-note", "Counted from the database. Nothing inferred."));
  observedSection.appendChild(observedHead);

  const counts = el("div", "facts");
  counts.appendChild(fact("Payments observed", report.observed.payments_observed));
  counts.appendChild(fact("Failed", report.observed.failed));
  counts.appendChild(fact("Captured", report.observed.captured));
  counts.appendChild(fact("Authorized, not captured", report.observed.authorized_not_captured));
  if (report.observed.orders_without_a_payment_attempt > 0) {
    counts.appendChild(
      fact("Orders with no payment yet", report.observed.orders_without_a_payment_attempt),
    );
  }
  observedSection.appendChild(counts);

  const reasons = Object.entries(report.observed.failure_reason_counts || {});
  if (reasons.length) {
    const reasonWrap = el("div", "reasons");
    reasonWrap.appendChild(el("span", "reasons-label", "Failure reasons Razorpay reported"));
    const chips = el("div", "chips");
    reasons
      .sort((a, b) => b[1] - a[1])
      .forEach(([reason, count]) => chips.appendChild(el("span", "chip", `${reason} × ${count}`)));
    reasonWrap.appendChild(chips);
    observedSection.appendChild(reasonWrap);
  }
  wrap.appendChild(observedSection);

  // ---- 2. Computed ----
  const computedSection = el("section", "layer layer-computed");
  const computedHead = el("div", "layer-head");
  computedHead.appendChild(el("span", "layer-tag", "Computed"));
  computedHead.appendChild(el("span", "layer-note", "Arithmetic on the counts above."));
  computedSection.appendChild(computedHead);

  const computedFacts = el("div", "facts");
  computedFacts.appendChild(
    fact(
      "Failure rate",
      report.computed.failure_rate === null || report.computed.failure_rate === undefined
        ? `Not computed (needs at least ${report.thresholds.min_observations_for_a_rate} payments)`
        : `${percent(report.computed.failure_rate)} (${report.observed.failed} of ${report.observed.payments_observed})`,
    ),
  );
  if (report.computed.failure_window_seconds !== null && report.computed.failure_window_seconds !== undefined) {
    computedFacts.appendChild(
      fact("Failures spread over", `${Math.round(report.computed.failure_window_seconds)} seconds`),
    );
  }
  if (report.computed.dominant_failure_reason) {
    computedFacts.appendChild(
      fact(
        "Most common failure reason",
        `${report.computed.dominant_failure_reason} (${percent(report.computed.dominant_failure_reason_share)} of failures)`,
      ),
    );
  }
  computedSection.appendChild(computedFacts);
  wrap.appendChild(computedSection);

  // ---- 3. Interpretation ----
  const interpretationSection = el("section", "layer layer-interpretation");
  const interpretationHead = el("div", "layer-head");
  interpretationHead.appendChild(el("span", "layer-tag", "Interpretation"));
  interpretationHead.appendChild(el("span", "layer-note", "What the numbers above may mean."));
  interpretationSection.appendChild(interpretationHead);

  interpretationSection.appendChild(
    pill(report.interpretation.code.replace(/_/g, " "), TONE_BY_CODE[report.interpretation.code] || "neutral"),
  );
  interpretationSection.appendChild(el("p", "interpretation-headline", report.interpretation.headline));
  interpretationSection.appendChild(el("p", "interpretation-detail", report.interpretation.detail));

  const limits = el("div", "limitations");
  limits.appendChild(el("span", "limitations-label", "What this cannot tell you"));
  const list = el("ul", "limitations-list");
  (report.interpretation.limitations || []).forEach((text) => list.appendChild(el("li", null, text)));
  limits.appendChild(list);
  interpretationSection.appendChild(limits);
  wrap.appendChild(interpretationSection);

  wrap.appendChild(
    technicalDetails([
      ["analysis_version", report.analysis_version],
      ["scope", report.scope],
      ["scope_id", report.scope_id],
      ["concentration_rate_threshold", report.thresholds.concentration_rate_threshold],
      ["min_observations_for_a_rate", report.thresholds.min_observations_for_a_rate],
      ["min_failures_for_pattern", report.thresholds.min_failures_for_pattern],
      ["first_failure_at", report.observed.first_failure_at],
      ["last_failure_at", report.observed.last_failure_at],
      ["interpretation_code", report.interpretation.code],
      ["consistent_with_wider_problem", String(report.interpretation.consistent_with_wider_problem)],
    ]),
  );

  return wrap;
}
