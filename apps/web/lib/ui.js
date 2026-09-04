"use strict";

/**
 * Shared rendering helpers.
 *
 * One rule runs through all of this: a value that was not observed is
 * rendered as "not observed", never as a default that looks like a
 * measurement. Anything showing a payment state, a decision, or an outcome
 * takes it from the API response and nowhere else.
 */

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function rupees(paise) {
  if (paise === null || paise === undefined) return "—";
  return `₹${(paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function percent(fraction) {
  if (fraction === null || fraction === undefined) return "—";
  return `${Math.round(fraction * 100)}%`;
}

/** Payment states as Razorpay reports them. */
const PAYMENT_TONE = {
  captured: "good",
  authorized: "attention",
  failed: "bad",
  refunded: "neutral",
  created: "neutral",
};

export function paymentTone(status) {
  return PAYMENT_TONE[status] || "neutral";
}

/** Pipeline/action statuses as this system records them. */
const STATUS_TONE = {
  VERIFIED_SUCCESS: "good",
  VERIFIED_FAILED: "bad",
  BLOCKED: "bad",
  ESCALATED: "attention",
  APPROVAL_PENDING: "attention",
  VERIFYING: "attention",
  VERIFICATION_UNCERTAIN: "attention",
  AUTHORIZED: "neutral",
  EXECUTING: "neutral",
  EXECUTED: "neutral",
  NO_ACTION: "neutral",
  RECOMMEND_CAPTURE: "neutral",
  RECOMMEND_RETRY_PROMPT: "neutral",
  RECOMMEND_ESCALATION: "attention",
  RECOMMEND_STOP: "attention",
};

export function statusTone(status) {
  return STATUS_TONE[status] || "neutral";
}

export function pill(text, tone) {
  return el("span", `pill pill-${tone || "neutral"}`, text);
}

/** A labelled fact. Used everywhere an observed value is shown. */
export function fact(label, value, tone) {
  const box = el("div", "fact");
  box.appendChild(el("span", "fact-label", label));
  const valueNode = el("span", `fact-value${tone ? ` fact-${tone}` : ""}`);
  valueNode.textContent = value === null || value === undefined || value === "" ? "Not observed" : String(value);
  box.appendChild(valueNode);
  return box;
}

/**
 * Progressive disclosure for the technical evidence. Collapsed by default:
 * the primary experience is for a person reading plain sentences, and the
 * ids and reason codes are there for someone who wants to check them.
 */
export function technicalDetails(rows, summaryLabel = "View technical details") {
  const wrap = el("details", "tech");
  wrap.appendChild(el("summary", null, summaryLabel));
  const table = el("div", "tech-rows");
  rows
    .filter(([, value]) => value !== undefined)
    .forEach(([key, value]) => {
      const row = el("div", "tech-row");
      row.appendChild(el("span", "tech-key", key));
      row.appendChild(
        el("span", "tech-value", value === null || value === "" ? "—" : String(value)),
      );
      table.appendChild(row);
    });
  wrap.appendChild(table);
  return wrap;
}

/**
 * The step scaffold every journey uses: a number, a title, one instruction,
 * and exactly one obvious next action.
 */
export function step({ number, title, body, state }) {
  const node = el("section", `step step-${state || "active"}`);
  const head = el("div", "step-head");
  head.appendChild(el("span", "step-number", String(number).padStart(2, "0")));
  head.appendChild(el("h2", "step-title", title));
  node.appendChild(head);
  const content = el("div", "step-body");
  if (body) content.appendChild(body);
  node.appendChild(content);
  return { node, content };
}

export function primaryButton(label, onClick) {
  const button = el("button", "btn btn-primary", label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

export function secondaryButton(label, onClick) {
  const button = el("button", "btn btn-secondary", label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

export function notice(text, tone = "neutral") {
  return el("p", `notice notice-${tone}`, text);
}

/**
 * A live status line for an operation that is genuinely running.
 *
 * The caller updates it as real stages complete. It is never advanced on a
 * timer: if the backend has not reported a stage, the line does not claim
 * it happened.
 */
export function statusLine() {
  const node = el("p", "status-line");
  return {
    node,
    set(text) {
      node.textContent = text;
    },
    clear() {
      node.textContent = "";
    },
  };
}

/**
 * The pipeline as a person reads it. Each stage is either something the
 * backend reported, or explicitly "not reached".
 */
export function pipelineTrack(stages) {
  const track = el("div", "track");
  stages.forEach((stage) => {
    const item = el("div", `track-stage track-${stage.state}`);
    item.appendChild(el("span", "track-name", stage.name));
    item.appendChild(el("span", "track-value", stage.value));
    if (stage.note) item.appendChild(el("span", "track-note", stage.note));
    track.appendChild(item);
  });
  return track;
}

/** Audit checkpoints in plain language, keyed on the real checkpoint names. */
export const CHECKPOINT_MEANING = {
  EVENT_INGESTED: "A payment state change was recorded from Razorpay.",
  AI_DIAGNOSIS_RECORDED: "The failure classifier's output was recorded.",
  DECISION_CREATED: "The system recorded what it recommends.",
  POLICY_EVALUATED: "Merchant policy decided whether the action was allowed.",
  ACTION_AUTHORIZED: "The action was cleared to run.",
  ACTION_BLOCKED: "Policy stopped the action. Nothing was sent to Razorpay.",
  APPROVAL_PENDING: "The action needs a human approval before it can run.",
  APPROVAL_GRANTED: "A human approved the action.",
  ACTION_EXECUTED: "The request was sent to Razorpay.",
  VERIFICATION_COMPLETED: "Razorpay was read again to confirm what actually happened.",
  RECONCILIATION_ANOMALY: "An unexpected state change was recorded rather than guessed at.",
};

export function auditTimeline(entries) {
  const list = el("ol", "audit");
  if (!entries || entries.length === 0) {
    list.appendChild(el("li", "audit-empty", "No audit entries recorded yet."));
    return list;
  }
  entries.forEach((entry, index) => {
    const item = el("li", "audit-item");
    const head = el("div", "audit-head");
    head.appendChild(el("span", "audit-index", String(index + 1).padStart(2, "0")));
    head.appendChild(el("span", "audit-checkpoint", entry.checkpoint));
    item.appendChild(head);
    item.appendChild(
      el("p", "audit-meaning", CHECKPOINT_MEANING[entry.checkpoint] || "A checkpoint was recorded."),
    );
    if (entry.snapshot && Object.keys(entry.snapshot).length > 0) {
      const pre = el("pre", "audit-payload");
      pre.textContent = JSON.stringify(entry.snapshot, null, 2);
      const details = el("details", "tech");
      details.appendChild(el("summary", null, "Recorded payload"));
      details.appendChild(pre);
      item.appendChild(details);
    }
    list.appendChild(item);
  });
  return list;
}

/** Cross-links to the other three problems, shown at the end of a journey. */
export function otherProblems(currentId, problems) {
  const wrap = el("section", "other-problems");
  wrap.appendChild(el("h2", "other-title", "Try another problem"));
  const grid = el("div", "other-grid");
  problems
    .filter((p) => p.id !== currentId)
    .forEach((p) => {
      const link = el("a", "other-card");
      link.href = `#/problem/${p.id}`;
      link.appendChild(el("span", "other-number", p.number));
      link.appendChild(el("span", "other-question", p.question));
      grid.appendChild(link);
    });
  wrap.appendChild(grid);
  return wrap;
}
