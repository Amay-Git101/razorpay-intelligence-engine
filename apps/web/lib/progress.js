"use strict";

/**
 * Translates a timeline snapshot into stage states and live events.
 *
 * Shared by the two journeys that run the full pipeline, so both describe
 * the same backend state in the same words.
 *
 * Every function here is a pure reading of what the API returned. A stage
 * the backend has not reached stays `waiting`; it is never advanced
 * because time passed or because a neighbouring stage completed.
 */

export const PIPELINE_STAGES = [
  { id: "order", name: "Order created" },
  { id: "payment", name: "Payment observed" },
  { id: "decision", name: "System recommendation" },
  { id: "policy", name: "Policy decision" },
  { id: "action", name: "Action" },
  { id: "verification", name: "Verification" },
];

export function latestAttempt(timeline) {
  if (!timeline || !timeline.payment_attempts || !timeline.payment_attempts.length) return null;
  return timeline.payment_attempts[timeline.payment_attempts.length - 1];
}

function paymentStageState(attempt) {
  if (!attempt) return ["waiting", ""];
  if (attempt.status === "captured") return ["done", "Captured"];
  if (attempt.status === "authorized" && !attempt.captured) {
    return ["attention", "Authorized — money not taken yet"];
  }
  if (attempt.status === "failed") {
    return ["blocked", attempt.error_reason ? `Failed — ${attempt.error_reason}` : "Failed"];
  }
  return ["done", attempt.status];
}

function policyStageState(policy) {
  if (!policy) return ["waiting", ""];
  if (policy.allowed === false) return ["blocked", "Blocked — automatic capture not allowed"];
  if (policy.requires_approval) return ["attention", "Approval required before acting"];
  return ["done", "Allowed"];
}

function actionStageState(action) {
  if (!action) return ["waiting", ""];
  if (action.status === "BLOCKED") return ["blocked", "Not executed — nothing sent to Razorpay"];
  if (action.status === "APPROVAL_PENDING") return ["attention", "Waiting for a human approval"];
  if (action.status === "VERIFIED_SUCCESS" || action.status === "EXECUTED") {
    return ["done", "Capture request sent"];
  }
  return ["done", action.status];
}

function verificationStageState(verification) {
  if (!verification) return ["waiting", ""];
  if (verification.result === "VERIFIED_SUCCESS") return ["done", "Razorpay confirmed the capture"];
  if (verification.result === "VERIFIED_FAILED") return ["blocked", "Razorpay says it is not captured"];
  return ["attention", verification.result];
}

/**
 * Applies a snapshot to the track and pushes one event per stage that has
 * become known since the last snapshot.
 *
 * `seen` is a Set the caller owns, so an event is announced once rather
 * than on every poll.
 */
export function applyTimeline(track, stream, timeline, seen) {
  const announce = (key, text, tone) => {
    if (seen.has(key)) return;
    seen.add(key);
    if (stream) stream.push(text, tone);
  };

  const attempt = latestAttempt(timeline);
  const [paymentState, paymentDetail] = paymentStageState(attempt);
  track.set("payment", paymentState, paymentDetail);
  if (attempt) {
    announce(
      `payment:${attempt.id}:${attempt.status}`,
      attempt.status === "authorized" && !attempt.captured
        ? "Payment authorized. The money has not been captured yet."
        : attempt.status === "captured"
          ? "Payment observed as captured."
          : attempt.status === "failed"
            ? `Payment failed${attempt.error_reason ? ` — ${attempt.error_reason}` : "."}`
            : `Payment observed as ${attempt.status}.`,
      attempt.status === "failed" ? "bad" : "good",
    );
  }

  const decision = timeline.decision;
  if (decision) {
    track.set("decision", "done", decisionWord(decision.decision_type));
    announce(`decision:${decision.id}`, `Recommendation recorded: ${decisionWord(decision.decision_type)}.`);
  }

  const [policyState, policyDetail] = policyStageState(timeline.policy);
  track.set("policy", policyState, policyDetail);
  if (timeline.policy) {
    announce(
      `policy:${decision ? decision.id : "x"}`,
      `Policy checked: ${policyDetail.toLowerCase()}.`,
      timeline.policy.allowed === false ? "bad" : "good",
    );
  }

  const [actionState, actionDetail] = actionStageState(timeline.action);
  track.set("action", actionState, actionDetail);
  if (timeline.action) {
    announce(`action:${timeline.action.id}`, `${actionDetail}.`, timeline.action.status === "BLOCKED" ? "bad" : null);
  }

  const [verifyState, verifyDetail] = verificationStageState(timeline.verification);
  track.set("verification", verifyState, verifyDetail);
  if (timeline.verification) {
    announce(
      `verify:${timeline.verification.result}:${timeline.action ? timeline.action.id : "x"}`,
      `${verifyDetail}.`,
      timeline.verification.result === "VERIFIED_SUCCESS" ? "good" : "bad",
    );
  }
}

export function decisionWord(decisionType) {
  switch (decisionType) {
    case "RECOMMEND_CAPTURE":
      return "Capture this payment";
    case "RECOMMEND_RETRY_PROMPT":
      return "Ask the customer to try again";
    case "RECOMMEND_ESCALATION":
      return "Send to a person";
    case "RECOMMEND_STOP":
      return "Stop chasing this payment";
    case "NO_ACTION":
      return "Nothing to do";
    default:
      return decisionType;
  }
}

/**
 * The one-line headline for a finished run. Deliberately refuses to call
 * anything a success unless VERIFICATION said so.
 */
export function outcomeHeadline(timeline) {
  const verification = timeline.verification;
  const outcome = timeline.outcome;

  if (verification && verification.result === "VERIFIED_SUCCESS" && outcome) {
    return { tone: "good", title: "Verified success", detail: null, amount: outcome.recovered_amount };
  }
  if (timeline.policy && timeline.policy.allowed === false) {
    return {
      tone: "blocked",
      title: "Policy blocked the action",
      detail: "The system recommended capture. Policy did not allow it, so no money moved.",
    };
  }
  if (timeline.action && timeline.action.status === "APPROVAL_PENDING") {
    return {
      tone: "attention",
      title: "Waiting for approval",
      detail: "The system recommended capture, but this amount needs a person to approve it first. No money moved.",
    };
  }
  if (verification && verification.result === "VERIFIED_FAILED") {
    return {
      tone: "blocked",
      title: "Not captured",
      detail: "Razorpay was read again and reports this payment is not captured.",
    };
  }
  if (verification) {
    return {
      tone: "attention",
      title: "Final state not confirmed",
      detail: "Verification could not establish a definite outcome, so this is escalated rather than assumed.",
    };
  }
  if (timeline.decision && timeline.decision.decision_type === "NO_ACTION") {
    return {
      tone: "neutral",
      title: "Nothing to do",
      detail: "There was no action to take on this payment, so no policy check or capture was needed.",
    };
  }
  return { tone: "neutral", title: "No action taken", detail: "The pipeline did not reach an action for this payment." };
}
