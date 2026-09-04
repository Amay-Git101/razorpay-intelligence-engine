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
    question: "An authorized payment needs a decision",
    summary: "Razorpay is holding the money but has not taken it. Capture it, or not?",
    detail:
      "You will create a real Test Mode order, pay it, and watch the system read the payment, decide what it recommends, check merchant policy independently, act only if policy allows, and then verify with Razorpay what actually happened.",
    render: renderCaptureJourney,
  },
  {
    id: 2,
    number: "02",
    question: "Is the payment gateway having trouble?",
    summary: "Failures happen. The question is whether these failures look ordinary.",
    detail:
      "This counts what this merchant's recent payments actually did, computes a failure rate, and keeps what was observed separate from what it might mean. It will not claim an outage it cannot see.",
    render: renderGatewayJourney,
  },
  {
    id: 3,
    number: "03",
    question: "Is this one payment failing, or are many failing?",
    summary: "Create six real payments, drive them yourself, and see what the group says.",
    detail:
      "Six real Test Mode orders are created and fixed as a group before any of them is paid. You decide which succeed and which fail. The conclusion is computed from those six, and it changes when your results change.",
    render: renderCohortJourney,
  },
  {
    id: 4,
    number: "04",
    question: "Does the customer's previous payment behaviour change the decision?",
    summary: "The same failure can deserve a different response depending on what came before.",
    detail:
      "Pay as the same customer more than once and the system reads that real history when it decides. History can send a payment to a human for review; it can never buy a payment more automation.",
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

function renderLanding() {
  const wrap = el("div", "landing");

  const hero = el("section", "hero");
  hero.appendChild(el("h1", "hero-title", "What payment problem do you want to test?"));
  hero.appendChild(
    el(
      "p",
      "hero-sub",
      "Four problems, each one an experiment you run yourself against Razorpay Test Mode. You create the payments, you decide what happens to them, and the system responds to what you actually did.",
    ),
  );
  wrap.appendChild(hero);

  const grid = el("div", "problem-grid");
  PROBLEMS.forEach((problem) => {
    const card = el("a", "problem-card");
    card.href = `#/problem/${problem.id}`;
    card.appendChild(el("span", "problem-number", problem.number));
    card.appendChild(el("h2", "problem-question", problem.question));
    card.appendChild(el("p", "problem-summary", problem.summary));
    card.appendChild(el("span", "problem-go", "Run this experiment →"));
    grid.appendChild(card);
  });
  wrap.appendChild(grid);

  return wrap;
}

function renderProblem(problem) {
  const wrap = el("div", "problem-view");

  const back = el("a", "back-link", "← All problems");
  back.href = "#/";
  wrap.appendChild(back);

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
