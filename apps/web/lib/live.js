"use strict";

import { el } from "./ui.js";

/**
 * The pieces that make the page move in response to real events.
 *
 * The rule every one of these obeys: a stage only advances when the caller
 * has actually learned something from the backend. Nothing here runs on a
 * timer, and no function in this file can advance a stage by itself. The
 * transitions are animated, but what they animate is real state arriving.
 */

// ---------------------------------------------------------------------------
// Stage track
// ---------------------------------------------------------------------------

const GLYPH = {
  waiting: "○",
  working: "●",
  done: "✓",
  blocked: "✕",
  attention: "!",
  skipped: "–",
};

/**
 * @param {{id: string, name: string}[]} stageDefs
 *
 * Returns a track whose stages start as `waiting`. The caller sets each one
 * as it genuinely learns the answer -- `working` while a request is in
 * flight, then the real terminal state.
 */
export function createStageTrack(stageDefs) {
  const node = el("ol", "stagetrack");
  node.setAttribute("aria-label", "Pipeline progress");
  const rows = new Map();

  stageDefs.forEach((stage) => {
    const row = el("li", "stage stage-waiting");
    row.dataset.stage = stage.id;

    const mark = el("span", "stage-mark", GLYPH.waiting);
    mark.setAttribute("aria-hidden", "true");
    row.appendChild(mark);

    const body = el("div", "stage-body");
    body.appendChild(el("span", "stage-name", stage.name));
    const detail = el("span", "stage-detail", "");
    body.appendChild(detail);
    row.appendChild(body);

    node.appendChild(row);
    rows.set(stage.id, { row, mark, detail });
  });

  function set(id, state, detail) {
    const entry = rows.get(id);
    if (!entry) return;
    entry.row.className = `stage stage-${state}`;
    entry.mark.textContent = GLYPH[state] || GLYPH.waiting;
    entry.detail.textContent = detail || "";
    // Status is carried in text as well as colour, so it survives both
    // colour-blindness and a screen reader.
    entry.row.setAttribute("aria-label", `${entry.row.querySelector(".stage-name").textContent}: ${state}. ${detail || ""}`);
  }

  function reset() {
    stageDefs.forEach((stage) => set(stage.id, "waiting", ""));
  }

  return { node, set, reset };
}

// ---------------------------------------------------------------------------
// Event stream
// ---------------------------------------------------------------------------

function clockTime() {
  return new Date().toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * A running log of what this page has actually observed.
 *
 * Timestamps are the moment the PAGE learned a thing, which is why the
 * heading says so. Inventing backend timestamps would be worse than
 * useless -- the audit trail carries the backend's own recorded order, and
 * that is where a reader should go for it.
 */
export function createEventStream() {
  const wrap = el("section", "stream");
  const head = el("div", "stream-head");
  head.appendChild(el("h3", "stream-title", "Live activity"));
  head.appendChild(el("span", "stream-note", "as this page observed it"));
  wrap.appendChild(head);

  const list = el("ol", "stream-list");
  list.setAttribute("aria-live", "polite");
  wrap.appendChild(list);

  const empty = el("p", "stream-empty", "Nothing has happened yet.");
  wrap.appendChild(empty);

  function push(text, tone) {
    empty.hidden = true;
    const item = el("li", `stream-item${tone ? ` stream-${tone}` : ""}`);
    item.appendChild(el("span", "stream-time", clockTime()));
    item.appendChild(el("span", "stream-text", text));
    list.appendChild(item);
    // Only the newest entry animates; older ones stay put rather than
    // re-animating every time something arrives.
    requestAnimationFrame(() => item.classList.add("is-in"));
    list.scrollTop = list.scrollHeight;
  }

  function clear() {
    while (list.firstChild) list.removeChild(list.firstChild);
    empty.hidden = false;
  }

  return { node: wrap, push, clear };
}

// ---------------------------------------------------------------------------
// Attention
// ---------------------------------------------------------------------------

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Brings the next thing to do into view without yanking the page around.
 * Skipped entirely when the element is already comfortably visible.
 */
export function focusOn(node) {
  if (!node) return;
  const rect = node.getBoundingClientRect();
  const alreadyVisible = rect.top >= 60 && rect.bottom <= window.innerHeight;
  if (alreadyVisible) return;
  node.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "center",
  });
}

// ---------------------------------------------------------------------------
// Buttons that show their own work
// ---------------------------------------------------------------------------

/**
 * Wraps a click handler so the button reports what it is doing.
 *
 * The working label is shown for exactly as long as the real work takes --
 * there is no minimum display time, because padding a fast response to
 * make it look busy is the same lie as a fake progress bar.
 */
export function actionButton({ label, workingLabel, doneLabel, onClick, variant = "primary" }) {
  const button = el("button", `btn btn-${variant}`, label);
  button.type = "button";

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.classList.add("is-working");
    if (workingLabel) button.textContent = workingLabel;
    try {
      await onClick();
      if (doneLabel) {
        button.textContent = doneLabel;
        button.classList.remove("is-working");
        button.classList.add("is-done");
        return; // stays disabled: the work it names is finished
      }
    } catch (error) {
      button.textContent = label;
      throw error;
    } finally {
      button.classList.remove("is-working");
      if (!button.classList.contains("is-done")) {
        button.disabled = false;
        if (!doneLabel) button.textContent = label;
      }
    }
  });

  return button;
}

/**
 * Polls a read endpoint while a real operation is in flight, so multi-step
 * backend work becomes visible as it commits rather than all at once.
 *
 * Stops the moment the operation settles. There is no idle polling
 * anywhere in this app.
 */
export async function pollWhile(operation, { poll, onUpdate, intervalMs = 800 }) {
  let running = true;
  const loop = (async () => {
    while (running) {
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      if (!running) break;
      try {
        const snapshot = await poll();
        if (running && snapshot) onUpdate(snapshot);
      } catch (_) {
        // A failed poll is not interesting: the operation itself is the
        // thing being awaited, and its result is authoritative.
      }
    }
  })();

  try {
    return await operation;
  } finally {
    running = false;
    await loop;
  }
}
