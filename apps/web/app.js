"use strict";

import { Api } from "./lib/api.js";
import { clear, el, notice, otherProblems } from "./lib/ui.js";
import { renderCaptureJourney } from "./journeys/capture.js";
import { renderGatewayJourney } from "./journeys/gateway.js";
import { renderCohortJourney } from "./journeys/cohort.js";
import { renderHistoryJourney } from "./journeys/history.js";

/**
 * The whole site is four guided experiments. This file routes between them
 * and resolves the merchant they run against; everything that decides
 * anything lives on the server.
 */

const PROBLEMS = [
  {
    id: 1,
    number: "01",
    navLabel: "Decision",
    question: "An authorized payment needs a decision",
    summary: "Razorpay is holding the money but has not taken it. Capture it, or not?",
    doing: "Create a real test payment and watch the system decide whether it should be captured.",
    detail:
      "Razorpay can hold an authorized payment without taking the money. Something has to decide whether to take it — and whether it is allowed to.",
    render: renderCaptureJourney,
  },
  {
    id: 2,
    number: "02",
    navLabel: "Gateway",
    question: "Is the payment gateway having trouble?",
    summary: "Failures happen. The question is whether these failures look ordinary.",
    doing: "Use real payment activity to see whether failures are isolated or becoming unusual.",
    detail:
      "A failure on its own means little. The question is whether these failures look ordinary — and what this system can honestly conclude from its own data.",
    render: renderGatewayJourney,
  },
  {
    id: 3,
    number: "03",
    navLabel: "Payment pattern",
    question: "Is this one payment failing, or are many failing?",
    summary: "Create six real payments, drive them yourself, and see what the group says.",
    doing: "Create six real payments, complete them yourself, and see what pattern the results form.",
    detail:
      "Six real orders, fixed as a group before any of them is paid. You decide which succeed and which fail; the conclusion follows your results.",
    render: renderCohortJourney,
  },
  {
    id: 4,
    number: "04",
    navLabel: "Customer history",
    question: "Does the customer's previous payment behaviour change the decision?",
    summary: "The same failure can deserve a different response depending on what came before.",
    doing: "Use the same customer across payments and see what the system learns from actual history.",
    detail:
      "The same failure can deserve a different response depending on what that customer did before. Here the history is real, so you build it yourself.",
    render: renderHistoryJourney,
  },
];

const root = document.getElementById("root");
const healthText = document.getElementById("health-text");
const healthNode = document.getElementById("health");

const ctx = { merchant: null };

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------

async function refreshHealth() {
  try {
    await Api.health();
    healthNode.classList.add("ok");
    healthText.textContent = "API healthy";
  } catch (_) {
    healthNode.classList.add("down");
    healthText.textContent = "API unreachable";
  }
}

/**
 * Which merchant the experiments run against.
 *
 * The journeys need a merchant whose policy is actually configured, or the
 * capture decision has no limits to be judged against. The demo merchant is
 * preferred by name and the most recent merchant is the fallback; either
 * way the name is shown in the header, so the merchant in use is never
 * hidden from the person running the experiment.
 */
async function resolveMerchant() {
  const response = await Api.merchants();
  const merchants = response.merchants || [];
  if (merchants.length === 0) return null;
  return merchants.find((m) => m.name === "Demo Live Merchant") || merchants[0];
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

/**
 * Razorpay's mark, drawn rather than fetched: the same shape as this page's
 * icon, as a stroked path so it can draw itself in. Purely decorative --
 * it is aria-hidden and carries no information.
 */
function razorpayMark() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "hero-mark");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M26 68 L26 32 L52 32 Q66 32 66 46 Q66 58 52 58 L38 58 L74 68");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke-width", "9");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  return svg;
}

function renderLanding() {
  const wrap = el("div", "landing");

  const hero = el("section", "hero");

  // Decoration, all of it aria-hidden and none of it carrying meaning: a
  // ruled grid, a glow that follows the pointer, and Razorpay's mark drawn
  // as a stroke. Nothing here says anything, so nothing here can say
  // anything untrue.
  hero.appendChild(el("div", "hero-grid")).setAttribute("aria-hidden", "true");
  const glow = el("div", "hero-glow");
  glow.setAttribute("aria-hidden", "true");
  hero.appendChild(glow);
  hero.appendChild(razorpayMark());

  const body = el("div", "hero-body");
  // Two-tone headline: the subject of the page is the coloured half.
  const title = el("h1", "hero-title", "What ");
  title.appendChild(el("em", null, "payment problem"));
  title.appendChild(document.createTextNode(" do you want to test?"));
  body.appendChild(title);
  hero.appendChild(body);

  // The glow tracks the pointer through two custom properties. Pointer
  // position is the only thing it knows.
  hero.addEventListener("pointermove", (event) => {
    const box = hero.getBoundingClientRect();
    hero.style.setProperty("--mx", `${event.clientX - box.left}px`);
    hero.style.setProperty("--my", `${event.clientY - box.top}px`);
  });
  hero.addEventListener("pointerleave", () => hero.classList.remove("is-lit"));
  hero.addEventListener("pointerenter", () => hero.classList.add("is-lit"));
  body.appendChild(
    el(
      "p",
      "hero-sub",
      "Four problems, each one an experiment you run yourself against Razorpay Test Mode. You create the payments, you decide what happens to them, and the system responds to what you actually did.",
    ),
  );
  wrap.appendChild(hero);

  // FAILS OPEN, for the same reason the scroll reveals in lib/live.js do.
  // The entrance animations start from opacity 0, so anything that stops
  // them running -- a throttled background tab, an embedded browser that
  // does not animate -- would leave the page blank. This forces the
  // finished state shortly after arrival regardless of what ran.
  setTimeout(() => wrap.classList.add("is-settled"), 1500);

  const grid = el("div", "problem-grid");
  PROBLEMS.forEach((problem, index) => {
    const card = el("a", "problem-card");
    card.href = `#/problem/${problem.id}`;
    card.appendChild(el("span", "problem-number", problem.number));
    card.appendChild(el("h2", "problem-question", problem.question));
    card.appendChild(el("p", "problem-summary", problem.doing));
    card.appendChild(el("span", "problem-go", "Start →"));

    // The card leans towards the pointer and lights up under it. Both are
    // driven by pointer position alone.
    card.addEventListener("pointermove", (event) => {
      const box = card.getBoundingClientRect();
      const px = (event.clientX - box.left) / box.width;
      const py = (event.clientY - box.top) / box.height;
      card.style.setProperty("--mx", `${px * 100}%`);
      card.style.setProperty("--my", `${py * 100}%`);
      card.style.setProperty("--tilt-y", `${(px - 0.5) * 7}deg`);
      card.style.setProperty("--tilt-x", `${(0.5 - py) * 7}deg`);
    });
    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--tilt-y", "0deg");
      card.style.setProperty("--tilt-x", "0deg");
    });

    // Entrance stagger. Presentation of static content, so a delay is the
    // right tool here -- unlike anything describing backend state.
    card.style.setProperty("--enter-delay", `${index * 70}ms`);
    grid.appendChild(card);
  });
  wrap.appendChild(grid);

  return wrap;
}

function renderProblem(problem) {
  const wrap = el("div", "problem-view");

  const nav = el("nav", "problem-nav");
  nav.setAttribute("aria-label", "Problems");
  const home = el("a", "problem-nav-home", "All problems");
  home.href = "#/";
  nav.appendChild(home);
  PROBLEMS.forEach((entry) => {
    const link = el("a", `problem-nav-item${entry.id === problem.id ? " is-active" : ""}`);
    link.href = `#/problem/${entry.id}`;
    link.appendChild(el("span", "problem-nav-number", entry.number));
    link.appendChild(el("span", "problem-nav-label", entry.navLabel));
    if (entry.id === problem.id) link.setAttribute("aria-current", "page");
    nav.appendChild(link);
  });
  wrap.appendChild(nav);

  const head = el("header", "problem-head");
  head.appendChild(el("span", "problem-number-lg", problem.number));
  head.appendChild(el("h1", "problem-title", problem.question));
  head.appendChild(el("p", "problem-detail", problem.detail));
  wrap.appendChild(head);

  const journeyRoot = el("div", "journey");
  wrap.appendChild(journeyRoot);

  const footer = el("div", "problem-footer");
  footer.appendChild(otherProblems(problem.id, PROBLEMS));
  wrap.appendChild(footer);

  return { wrap, journeyRoot };
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function currentRoute() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const match = hash.match(/^problem\/(\d)$/);
  if (match) return { view: "problem", id: Number(match[1]) };
  return { view: "landing" };
}

async function router() {
  const route = currentRoute();
  clear(root);
  window.scrollTo(0, 0);

  if (route.view === "landing") {
    root.appendChild(renderLanding());
    return;
  }

  const problem = PROBLEMS.find((p) => p.id === route.id);
  if (!problem) {
    root.appendChild(renderLanding());
    return;
  }

  const { wrap, journeyRoot } = renderProblem(problem);
  root.appendChild(wrap);

  if (!ctx.merchant) {
    journeyRoot.appendChild(
      notice(
        "No merchant is configured in this database, so there is nothing to run an experiment against.",
        "bad",
      ),
    );
    return;
  }

  problem.render(journeyRoot, ctx);
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

window.addEventListener("hashchange", router);

(async () => {
  refreshHealth();
  try {
    ctx.merchant = await resolveMerchant();
    if (ctx.merchant) {
      document.getElementById("env-pill").textContent = `Razorpay Test Mode · ${ctx.merchant.name}`;
    }
  } catch (_) {
    ctx.merchant = null;
  }
  router();
})();
