"use strict";

import {
  auditTimeline,
  el,
  fact,
  pill,
  pipelineTrack,
  rupees,
  statusTone,
  technicalDetails,
} from "./ui.js";

/**
 * Renders one order's real pipeline outcome, from GET /orders/{id}/timeline.
 *
 * Every stage below is drawn from a field the API returned. A stage the
 * pipeline never reached is drawn as "Not reached" -- there is no branch
 * here that fills a gap with something plausible. In particular:
 *
 *   - "Captured" is only ever shown when VERIFICATION says so. The action
 *     having executed is not treated as money having moved, because the
 *     whole point of the verification stage is that a write response is not
 *     proof.
 *   - A blocked action renders as blocked, and the action stage explicitly
 *     says nothing was sent to Razorpay.
 */

export function decisionSentence(decisionType) {
  switch (decisionType) {
    case "RECOMMEND_CAPTURE":
      return "The system recommends capturing this payment.";
    case "RECOMMEND_RETRY_PROMPT":
      return "The system recommends prompting the customer to try again.";
    case "RECOMMEND_ESCALATION":
      return "The system recommends sending this to a person to decide.";
    case "RECOMMEND_STOP":
      return "The system recommends stopping recovery on this payment.";
    case "NO_ACTION":
      return "The system found nothing to do here.";
    default:
      return `The system recorded ${decisionType}.`;
  }
}

export function policySentence(policy) {
  if (!policy) return "Policy was not consulted, because no action was proposed.";
  if (policy.allowed === false) return "Policy did not allow this action. No money moved.";
  if (policy.requires_approval) return "Policy allows this only with a human approval first.";
  return "Policy allows this action to run automatically.";
}

function policyOutcomeWord(policy) {
  if (!policy) return "Not reached";
  if (policy.allowed === false) return "BLOCK";
  if (policy.requires_approval) return "APPROVAL REQUIRED";
  return "ALLOW";
}

function policyTone(policy) {
  if (!policy) return "idle";
  if (policy.allowed === false) return "bad";
  if (policy.requires_approval) return "attention";
  return "good";
}

function verificationSentence(verification, outcome) {
  if (!verification) return "Verification has not run for this payment.";
  if (verification.result === "VERIFIED_SUCCESS") {
    const amount = outcome && outcome.recovered_amount !== undefined ? rupees(outcome.recovered_amount) : null;
    return amount
      ? `Razorpay was read again and confirms the payment is captured. ${amount} recovered.`
      : "Razorpay was read again and confirms the payment is captured.";
  }
  if (verification.result === "VERIFIED_FAILED") {
    return "Razorpay was read again and the payment is not captured.";
  }
  return "Verification could not establish a definite outcome. This is escalated rather than assumed.";
}

export function renderPipelineResult(timeline) {
  const wrap = el("div", "result");

  const attempt = timeline.payment_attempts && timeline.payment_attempts.length
    ? timeline.payment_attempts[timeline.payment_attempts.length - 1]
    : null;
  const decision = timeline.decision;
  const policy = timeline.policy;
  const action = timeline.action;
  const verification = timeline.verification;
  const outcome = timeline.outcome;

  // ---- The track a person reads first ----
  wrap.appendChild(
    pipelineTrack([
      {
        name: "Payment",
        value: attempt ? attempt.status : "No payment yet",
        state: attempt ? "done" : "idle",
      },
      {
        name: "System recommendation",
        value: decision ? decision.decision_type : "Not reached",
        state: decision ? "done" : "idle",
      },
      {
        name: "Policy decision",
        value: policyOutcomeWord(policy),
        state: policy ? "done" : "idle",
        note: policy ? undefined : "no action was proposed",
      },
      {
        name: "Action",
        value: action ? action.status : "Not reached",
        state: action ? "done" : "idle",
      },
      {
        name: "Verification",
        value: verification ? verification.result : "Not reached",
        state: verification ? "done" : "idle",
      },
      {
        name: "Result",
        value: outcome ? `${rupees(outcome.recovered_amount)} recovered` : "No money moved",
        state: outcome ? "done" : "idle",
      },
    ]),
  );

  // ---- The same thing in sentences ----
  const story = el("div", "story");

  if (attempt) {
    const line = el("p", "story-line");
    line.appendChild(el("strong", null, "Payment: "));
    line.append(
      attempt.status === "authorized" && !attempt.captured
        ? `Razorpay has authorized ${rupees(attempt.amount)} but the money has not been taken yet.`
        : attempt.status === "captured"
          ? `Razorpay reports this payment as captured (${rupees(attempt.amount)}).`
          : attempt.status === "failed"
            ? `This payment attempt failed${attempt.error_reason ? ` (${attempt.error_reason})` : ""}.`
            : `Razorpay reports this payment as ${attempt.status}.`,
    );
    story.appendChild(line);
  }

  if (decision) {
    const line = el("p", "story-line");
    line.appendChild(el("strong", null, "System recommendation: "));
    line.append(decisionSentence(decision.decision_type));
    story.appendChild(line);
  }

  const policyLine = el("p", "story-line");
  policyLine.appendChild(el("strong", null, "Policy decision: "));
  policyLine.append(policySentence(policy));
  story.appendChild(policyLine);

  if (action) {
    const line = el("p", "story-line");
    line.appendChild(el("strong", null, "Action: "));
    line.append(
      action.status === "BLOCKED"
        ? "Blocked. Nothing was sent to Razorpay."
        : `${action.action_type} — recorded as ${action.status}.`,
    );
    story.appendChild(line);
  } else {
    const line = el("p", "story-line");
    line.appendChild(el("strong", null, "Action: "));
    line.append("No action was taken.");
    story.appendChild(line);
  }

  const verifyLine = el("p", "story-line");
  verifyLine.appendChild(el("strong", null, "Verification: "));
  verifyLine.append(verificationSentence(verification, outcome));
  story.appendChild(verifyLine);

  wrap.appendChild(story);

  // ---- The separation the whole architecture is about ----
  if (decision && policy) {
    const split = el("div", "split");
    const left = el("div", "split-half");
    left.appendChild(el("span", "split-label", "What the system recommended"));
    left.appendChild(pill(decision.decision_type, statusTone(decision.decision_type)));
    left.appendChild(el("p", "split-note", "The engine can only recommend. It cannot move money."));
    const right = el("div", "split-half");
    right.appendChild(el("span", "split-label", "What policy allowed"));
    right.appendChild(pill(policyOutcomeWord(policy), policyTone(policy)));
    right.appendChild(el("p", "split-note", "Policy decides independently, from the merchant's own limits."));
    split.appendChild(left);
    split.appendChild(right);
    wrap.appendChild(split);
  }

  // ---- Facts ----
  const facts = el("div", "facts");
  facts.appendChild(fact("Order", timeline.order.id));
  facts.appendChild(fact("Amount", rupees(timeline.order.amount)));
  if (attempt) facts.appendChild(fact("Payment", attempt.id));
  if (attempt) facts.appendChild(fact("Payment state", attempt.status));
  if (attempt) facts.appendChild(fact("Captured flag", String(attempt.captured)));
  if (verification) facts.appendChild(fact("Verification", verification.result));
  wrap.appendChild(facts);

  // ---- Evidence ----
  wrap.appendChild(
    technicalDetails([
      ["order_id", timeline.order.id],
      ["payment_attempt_id", attempt ? attempt.id : null],
      ["decision_id", decision ? decision.id : null],
      ["decision_type", decision ? decision.decision_type : null],
      ["decision_reason_codes", decision ? decision.reason_codes.join(", ") : null],
      ["model_version", decision ? decision.model_version : null],
      ["policy_version", policy ? policy.policy_version : null],
      ["policy_allowed", policy ? String(policy.allowed) : null],
      ["policy_authority_level", policy ? policy.authority_level_granted : null],
      ["policy_reason_codes", policy ? policy.reason_codes.join(", ") : null],
      ["action_id", action ? action.id : null],
      ["action_type", action ? action.action_type : null],
      ["action_status", action ? action.status : null],
      ["execution_reference", action && action.execution_reference ? JSON.stringify(action.execution_reference) : null],
      ["verification_result", verification ? verification.result : null],
      ["verification_reason", verification ? verification.reason : null],
      ["verification_read_attempts", verification ? verification.attempt_count : null],
      ["recovered_amount", outcome ? outcome.recovered_amount : null],
    ]),
  );

  // ---- Audit trail ----
  const auditSection = el("section", "audit-section");
  auditSection.appendChild(el("h3", "sub-title", "What was recorded, in order"));
  auditSection.appendChild(
    el("p", "sub-note", "These entries were written by the backend as each stage happened. They are append-only."),
  );
  auditSection.appendChild(auditTimeline(timeline.audit));
  wrap.appendChild(auditSection);

  return wrap;
}
