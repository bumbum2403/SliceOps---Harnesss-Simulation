/* SliceOps: the shop floor.
 *
 * This file renders events. It never calls a model and it never computes game
 * logic - no prices, no totals, no decisions about what is on a pizza. If a
 * number appears on screen it arrived in an event or from /api/menu, which
 * serves the same shop/menu.json the sensors grade against. A price worked out
 * in JavaScript would be a second rounding rule in a second language.
 *
 * Three layers, in order:
 *   1. reduce()    - events in, UI state out
 *   2. beats()     - events in, a queue of timed stage directions out
 *   3. the pacer   - drains that queue at a speed a person can read
 *
 * The queue is the whole reason the animation is legible. Events arrive in
 * bursts (a replay sends its entire history at once, and a live step emits
 * five events in the millisecond after a model replies). Playing them as they
 * land produces exactly the flicker this was rebuilt to fix.
 */

/* ------------------------------------------------------------------ *
 * The floor plan
 * ------------------------------------------------------------------ */

/* Anchored on the horizon at y=560, give or take: a lower anchor reads as
 * nearer the viewer, which is how Marco ends up in front of the counter and
 * Sophia behind it without either of them being drawn twice. */
const SPOTS = {
  marco: {
    door:    [170, 585],
    board:   [550, 585],
    counter: [700, 700],   // well below the staff: in front of the counter
    pass:    [1240, 700],
    out:     [-260, 585],
  },
  sophia: { till: [920, 580] },
  chef:   { prep: [1520, 580], pass: [1380, 585] },
};

const AGENTS = ["marco", "sophia", "chef"];
const DEFAULT_CAST = { marco: "haiku", sophia: "sonnet", chef: "opus" };

/* How long a beat holds, before the speed control scales it. Tuned for
 * reading, not for pace: the complaint these replaced was that the shop moved
 * faster than anyone could follow. */
const MS = {
  walk: 1200,
  read: 1700,     // standing at the board, taking it in
  focus: 700,     // a step begins
  tool: 520,
  slip: 1500,     // a hand-off travelling between two workers
  cook: 1500,
  serve: 1300,
  pay: 1200,
  end: 900,
};

/* Someone who asked for reduced motion still wants to follow the shift, so
 * only the movement goes - every dwell below is untouched and the stage still
 * holds each line long enough to read. It also makes a headless screenshot
 * truthful: with motion off, what is painted is where things really are. */
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const secs = (ms) => (REDUCED ? 0 : ms / 1000);

gsap.defaults({ ease: "power2.out", overwrite: "auto" });


/** Dwell long enough to actually read `text`, within sane bounds. */
function readMs(text) {
  return Math.min(7000, Math.max(2200, 400 + String(text || "").length * 55));
}

/* ------------------------------------------------------------------ *
 * State
 * ------------------------------------------------------------------ */

let houseCast = { ...DEFAULT_CAST };

const state = {
  runId: null,
  phase: "DOOR",
  models: { ...DEFAULT_CAST },
  chain: [],
  menu: null,
  at: {},
  cost: 0,
  live: false,
  lastSummary: null,
  transcript: [],
  walkedIn: false,
};

const $ = (id) => document.getElementById(id);
const stage = $("stage");
const actor = (name) => $("actor-" + name);
const SVG = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const titleCase = (s) => String(s).replace(/\b\w/g, (c) => c.toUpperCase());

/* ------------------------------------------------------------------ *
 * Moving people about
 * ------------------------------------------------------------------ */

function place(name, spot) {
  const [x, y] = SPOTS[name][spot];
  gsap.set(actor(name), { x, y });
  state.at[name] = spot;
}

function spotOf(name) {
  return SPOTS[name][state.at[name]] || SPOTS[name][Object.keys(SPOTS[name])[0]];
}

/** Walk somewhere, swinging arms for as long as the walk lasts. */
function walk(name, spot, duration) {
  const el = actor(name);
  const [x, y] = SPOTS[name][spot];
  state.at[name] = spot;

  const seconds = secs(duration);
  const arms = el.querySelectorAll(".arm");
  const steps = Math.max(2, Math.round(seconds / 0.3)) || 1;

  // One timeline, everything placed at position 0, rather than three tweens
  // chained on delays: the arms are then guaranteed to stop swinging exactly
  // when the feet stop moving, at any duration.
  return gsap.timeline()
    .to(el, { x, y, duration: seconds, ease: "power1.inOut" }, 0)
    .to(arms[0], { rotation: 20, transformOrigin: "50% 8%",
                   duration: seconds / steps, repeat: steps - 1, yoyo: true,
                   ease: "sine.inOut" }, 0)
    .to(arms[1], { rotation: -20, transformOrigin: "50% 8%",
                   duration: seconds / steps, repeat: steps - 1, yoyo: true,
                   ease: "sine.inOut" }, 0)
    .set(arms, { rotation: 0 });
}

function wave(name) {
  if (REDUCED) return;
  const arm = actor(name).querySelector(".arm-r");
  gsap.timeline()
    .to(arm, { rotation: -145, transformOrigin: "50% 8%", duration: 0.32,
               ease: "back.out(2)" })
    .to(arm, { rotation: -115, duration: 0.2, yoyo: true, repeat: 3 })
    .to(arm, { rotation: 0, duration: 0.3, ease: "power2.inOut" });
}

function nod(name) {
  if (REDUCED) return;
  const head = actor(name).querySelector(".head");
  gsap.timeline()
    .to(head, { rotation: 11, duration: 0.24, yoyo: true, repeat: 3,
                ease: "sine.inOut" })
    .set(head, { rotation: 0 });
}

/** A little hop of effort: someone just did their job. */
function bounce(name) {
  if (REDUCED) return;
  const el = actor(name).querySelector(".body");
  gsap.timeline()
    .to(el, { scaleY: 0.92, scaleX: 1.05, transformOrigin: "50% 100%",
              duration: 0.14 })
    .to(el, { scaleY: 1, scaleX: 1, duration: 0.42,
              ease: "elastic.out(1, 0.45)" });
}

function spotlight(name) {
  // The class goes on the <svg>, not on a cast group: Marco is drawn in a
  // separate foreground layer, and a selector rooted at one group could only
  // ever dim half the shop.
  stage.classList.add("focused");
  for (const other of AGENTS) actor(other).classList.toggle("active", other === name);
}

function spotlightOff() {
  stage.classList.remove("focused");
  for (const name of AGENTS) actor(name).classList.remove("active");
}

/* ------------------------------------------------------------------ *
 * Speech
 * ------------------------------------------------------------------ */

/** A speech bubble, hand-wrapped because SVG text does not wrap itself. */
function bubble(name, text, ms) {
  const group = actor(name).querySelector(".bubble");
  const rect = group.querySelector("rect");
  const label = group.querySelector("text");

  const lines = [];
  let line = "";
  for (const word of String(text).split(/\s+/)) {
    if ((line + " " + word).trim().length > 32) { lines.push(line.trim()); line = word; }
    else line += " " + word;
  }
  if (line.trim()) lines.push(line.trim());

  const shown = lines.slice(0, 3);
  // Say so when there was more, rather than stopping mid-sentence. The rail
  // on the right has the whole line, so nothing is actually lost.
  if (lines.length > 3) shown[2] = shown[2].replace(/[,;]?$/, "") + "…";

  const width = Math.max(170, Math.min(330, 16 + Math.max(...shown.map((l) => l.length)) * 8));
  const height = 22 + shown.length * 21;
  // Clears the nameplate, which sits at rig y -138..-98.
  const top = -height - 152;

  rect.setAttribute("x", -width / 2);
  rect.setAttribute("y", top);
  rect.setAttribute("width", width);
  rect.setAttribute("height", height);

  label.textContent = "";
  label.setAttribute("text-anchor", "middle");
  shown.forEach((text, i) => {
    const tspan = svgEl("tspan", { x: 0, y: top + 26 + i * 21 }, text);
    label.appendChild(tspan);
  });

  // autoAlpha rather than opacity: at 0 it also sets visibility:hidden, so a
  // faded bubble cannot sit invisibly over the character underneath it.
  // One timeline rather than two tweens on a delay, so a bubble replaced
  // early cannot leave its own fade-out running over the next one.
  gsap.timeline()
    .fromTo(group, { autoAlpha: 0, y: 12 },
            { autoAlpha: 1, y: 0, duration: secs(300), ease: "back.out(1.6)" })
    .to(group, { autoAlpha: 0, duration: secs(300) },
        Math.max(secs(300), secs(ms) - secs(300)));
}


/* ------------------------------------------------------------------ *
 * Voices: what a tool call sounds like out loud
 *
 * Two different things end up on screen here, and the page keeps them
 * visually apart because they are not equally true.
 *
 *   - Words the MODEL wrote are used verbatim and shown in quotes:
 *     `customer_line` on an order, `reaction` at the window, and every
 *     hand-off line. Those are real natural language, written by that agent.
 *
 *   - Everything else is this file putting a sentence around arguments that
 *     are already in the event. Marco did not say "What have you got today?";
 *     he called get_menu. The renderer is dressing a tool call, not reporting
 *     speech, and it never invents a fact or computes one - every number
 *     below is read straight out of the payload.
 *
 * Under the dialogue, the raw tool name stays on each line, so what actually
 * happened is never more than a glance away.
 */

const quoted = (text) => ({ text: `\u201c${String(text).trim()}\u201d`, verbatim: true });
const said = (text) => ({ text, verbatim: false });

function describeItems(items) {
  return (items || []).map((item) => {
    const parts = [`${item.quantity || 1} ${item.pizza}`];
    if ((item.remove || []).length) parts.push(`no ${item.remove.join(" or ")}`);
    // Not "extra extra cheese": a topping already named "extra ..." is not
    // prefixed again.
    if ((item.extras || []).length) {
      parts.push("with " + item.extras
        .map((e) => (e.startsWith("extra") ? e : `extra ${e}`)).join(" and "));
    }
    return parts.join(", ");
  }).join("; ");
}

const WORKER_SAYS = {
  marco: {
    get_menu: () => said("What have you got today?"),
    place_order: (a) => (a.customer_line ? quoted(a.customer_line)
                                         : said(describeItems(a.items))),
    get_order: () => said("What do I owe you?"),
    pickup_order: (a) => quoted(a.reaction),
    pay: (a) => said(`Here you go \u2014 ${a.amount}.`),
  },
  sophia: {
    get_order: () => said("Let me see what you ordered."),
    get_menu: () => said("Checking the prices."),
    ring_up: (a) => said(`${a.subtotal} and ${a.tax} tax \u2014 ${a.total} altogether.`),
  },
  chef: {
    get_ticket: () => said("What\u2019s on the ticket?"),
    get_menu: () => said("Let me check the recipe."),
    serve_order: (a) => said(((a.lines || [])[0] || {}).ingredients
      ? `${a.lines[0].ingredients.join(", ")}. Order up.`
      : "Order up."),
  },
};

const SHOP_SAYS = {
  get_menu: (s) => `${(s.pizzas || []).length} pizzas, ${(s.toppings || []).length} `
    + `toppings. No ${(s.not_served || []).join(", ")}.`,
  place_order: (s) => `Order ${s.order_id} is in.`,
  get_order: (s) => `${s.order_id}: ${(s.items || []).join("; ")}.`
    + (s.total ? ` That comes to ${s.total}.` : ""),
  get_ticket: (s) => `${s.order_id}, ${(s.items || []).join("; ")}. Total ${s.total}.`,
  ring_up: (s) => `Ticket filed \u2014 ${s.total}.`,
  serve_order: (s) => (s.lines || []).join("; "),
  pickup_order: (s) => `That\u2019ll be ${s.amount_due}.`,
  pay: (s) => `Paid ${s.paid}. Thanks!`,
};

function workerLine(agent, tool, args) {
  const fn = (WORKER_SAYS[agent] || {})[tool];
  return fn ? fn(args || {}) : said(String(tool).replace(/_/g, " "));
}

function shopLine(tool, summary) {
  const fn = SHOP_SAYS[tool];
  try { return fn ? fn(summary || {}) : ""; } catch { return ""; }
}

/** Short enough not to drag over eleven calls, long enough to read. */
const sayMs = (text) =>
  Math.min(4500, Math.max(1300, 300 + String(text || "").length * 45));

/* ------------------------------------------------------------------ *
 * The hand-off slip: the one line one worker gives the next
 * ------------------------------------------------------------------ */

function slipFrom(fromAgent, toAgent, text, ms) {
  const slip = $("slip");
  const [fx] = spotOf(fromAgent);
  const [tx] = spotOf(toAgent);
  $("slip-text").textContent = text.length > 22 ? text.slice(0, 21) + "…" : text;

  const total = secs(ms);
  const pop = Math.min(0.3, total / 3);
  gsap.timeline()
    .set(slip, { x: fx, y: 440 })
    .fromTo(slip, { autoAlpha: 0, scale: 0.6 },
            { autoAlpha: 1, scale: 1, duration: pop, ease: "back.out(2)" })
    .to(slip, { x: tx, duration: Math.max(0, total - pop * 2),
                ease: "power2.inOut" })
    .to(slip, { autoAlpha: 0, scale: 0.6, duration: pop });
}

/* ------------------------------------------------------------------ *
 * The rail: the chain, and what everyone said
 * ------------------------------------------------------------------ */

function renderChain() {
  const root = $("chain");
  root.innerHTML = "";
  state.chain.forEach((step) => {
    const li = document.createElement("li");
    li.id = "chain-" + step.step;
    li.className = `${step.agent}-ink`;
    li.innerHTML = `
      <span class="n">${step.step}</span>
      <span class="who">${escapeHtml(step.agent)}</span>
      <span class="job">${escapeHtml(step.job)}</span>
      <span class="mdl">${escapeHtml(shortModel(state.models[step.agent]))}</span>`;
    root.appendChild(li);
  });
}

/** Exactly one step is active at a time, which is the point of a chain. */
function markStep(index, className) {
  // "done" does not clear "active": the step stays lit while its line is
  // still on screen, so the rail and the speech bubble agree about who you
  // are currently reading. The next step_started clears it.
  if (className === "active") {
    for (const li of document.querySelectorAll("#chain li.active")) {
      li.classList.remove("active");
    }
  }
  $("chain-" + index)?.classList.add(className);
}

function say(agent, text, kind = "") {
  return turn({ agent, text, kind });
}

/** One line in the conversation.
 *
 *  `tool` is printed small beside the speaker, so the dialogue never hides
 *  what actually happened: every dressed-up sentence sits next to the call it
 *  is dressing. `verbatim` marks words the model wrote itself.
 */
function turn({ agent, to, tool, text, kind = "", verbatim = false }) {
  if (!String(text || "").trim()) return;
  state.transcript.push({ agent, to, tool, text, kind });

  const root = $("transcript");
  root.querySelector(".empty")?.remove();

  const div = document.createElement("div");
  div.className = `line ${kind} ${verbatim ? "verbatim" : ""}`;
  const head = `<span class="who ${agent}-ink">${escapeHtml(agent)}</span>`
    + (to ? `<span class="to">\u2192 ${escapeHtml(to)}</span>` : "")
    + (tool ? `<span class="tool">${escapeHtml(tool)}</span>` : "");
  div.innerHTML = `<div class="head">${head}</div>
                   <div class="what">${escapeHtml(text)}</div>`;
  root.appendChild(div);
  root.scrollTop = root.scrollHeight;
  gsap.from(div, { autoAlpha: 0, y: 8, duration: secs(350) });
}

function shortModel(model) {
  return ["haiku", "sonnet", "opus"].find((f) => String(model).includes(f))
    || model || "?";
}

function syncPlates() {
  for (const name of AGENTS) {
    const badge = $("badge-" + name);
    if (badge) badge.textContent = shortModel(state.models[name]);
  }
}

function setPhase(phase) {
  state.phase = phase;
  $("phase-chip").textContent = phase;
}

/* ------------------------------------------------------------------ *
 * Layer 1: events in, UI state out
 * ------------------------------------------------------------------ */

/* Only what has to be true before the first beat plays.
 *
 * Everything else belongs in a beat. A replay delivers its whole history in
 * one burst, so anything updated here jumps straight to its final value while
 * the stage is still on step one - which is how the phase chip came to read
 * PAID over a Marco who had not ordered yet. If it is visible, it is a beat.
 */
function reduce(event) {
  const { type, payload } = event;

  if (type === "run_started") {
    state.runId = payload.run_id;
    $("run-id").textContent = payload.run_id;
    state.models = { ...houseCast, ...(payload.cast || {}) };
    state.chain = payload.chain || state.chain;
    syncPlates();
    renderChain();
  } else if (type === "run_finished" || type === "run_failed") {
    state.lastSummary = payload.summary || {};
  }
}

/* ------------------------------------------------------------------ *
 * Layer 2: events in, stage directions out
 *
 * One event can produce several beats, and some events produce none. Each
 * beat is {ms, run} - how long the stage holds, and what to do when it starts.
 * ------------------------------------------------------------------ */

function beats(event) {
  const { type, agent, payload } = event;
  const out = [];
  const push = (ms, run) => out.push({ ms, run });

  switch (type) {
    case "run_started":
      // Marco arrives, stops to read the board, then comes to the counter.
      // The pause at the board is not decoration: the menu on that wall is
      // where his prices come from, and the run shows him getting them.
      push(MS.walk, () => {
        resetStage();
        turn({ agent: "you", to: "marco", text: payload.order_text,
               kind: "note", verbatim: true });
        walk("marco", "board", MS.walk);
      });
      push(MS.read, () => {
        $("board").classList.add("lit");
        nod("marco");
      });
      push(MS.walk, () => {
        $("board").classList.remove("lit");
        walk("marco", "counter", MS.walk);
      });
      push(900, () => { wave("sophia"); nod("marco"); });
      break;

    // Zero-length: it costs the viewer no time, but queueing it keeps the
    // chip in step with the stage instead of with the network.
    case "state_changed":
      push(0, () => setPhase(payload.to_phase));
      break;

    case "step_started": {
      const who = payload.agent || agent;
      push(MS.focus, () => {
        spotlight(who);
        markStep(payload.step, "active");
        // Each worker goes to where their job happens. The chef cooks at the
        // bench and only comes to the pass to put the food down.
        if (who === "chef" && state.at.chef !== "prep") walk("chef", "prep", MS.focus);
        if (who === "marco" && payload.job === "pickup") walk("marco", "pass", MS.walk);
      });
      break;
    }

    case "tool_called": {
      const line = workerLine(agent, payload.tool, payload.args);
      const ms = sayMs(line.text);
      push(ms, () => {
        if (agent) bubble(agent, line.text, ms);
        turn({ agent, to: "shop", tool: payload.tool,
               text: line.text, verbatim: line.verbatim });
        if (payload.tool === "serve_order") $("oven-glow").setAttribute("opacity", "1");
      });
      break;
    }

    // The shop's half of the exchange. A short beat, not a read-length one:
    // it has no body to speak from, and the line stays in the rail to be read
    // at leisure.
    case "tool_returned": {
      const reply = shopLine(payload.tool, payload);
      if (reply) {
        push(620, () => turn({ agent: "shop", to: agent, text: reply, kind: "shop" }));
      }
      break;
    }

    case "order_note":
      for (const note of payload.notes || []) {
        push(readMs(note), () => say("sophia", note, "note"));
      }
      break;

    case "sensor_failed":
      push(readMs((payload.errors || [])[0]) + 400, () => {
        gsap.to(actor(agent), {
          keyframes: [{ rotation: -4 }, { rotation: 4 }, { rotation: 0 }],
          duration: secs(450), transformOrigin: "50% 100%",
        });
        say(agent, `${payload.tool} rejected (attempt ${payload.attempt}/3): `
                   + (payload.errors || [])[0], "fail");
      });
      break;

    case "ticket_rung":
      push(MS.serve, () => {
        $("till-total").textContent = payload.total;
        gsap.fromTo($("till-total"), { autoAlpha: 0, scale: 0.6 },
                    { autoAlpha: 1, scale: 1, duration: secs(500),
                      ease: "back.out(2)", transformOrigin: "50% 50%" });
        bounce("sophia");
      });
      break;

    case "order_served":
      push(MS.cook, () => { walk("chef", "pass", MS.cook); });
      push(MS.serve, () => {
        $("oven-glow").setAttribute("opacity", "0");
        showPizza(payload.lines || []);
        bounce("chef");
      });
      break;

    case "order_paid":
      push(MS.pay, () => {
        gsap.to($("plate-served"), { autoAlpha: 0, duration: secs(400) });
        gsap.to($("till-total"), { scale: 1.25, duration: secs(300),
                                   yoyo: true, repeat: 1,
                                   transformOrigin: "50% 50%" });
        bounce("marco");
      });
      break;

    case "step_finished": {
      const report = payload.report || "(said nothing)";
      push(readMs(report), () => {
        markStep(payload.step, "done");
        state.cost += Number(payload.cost_usd || 0);
        $("cost-chip").textContent = "$" + state.cost.toFixed(3);
        const who = payload.agent || agent;
        const next = state.chain.find((s) => s.step === payload.step + 1);
        bubble(who, report, readMs(report));
        // The report is the model's own words, and the only natural language
        // that actually crosses between two workers.
        turn({ agent: who, to: next ? next.agent : "", text: report,
               kind: "handoff", verbatim: true });
      });
      // The hand-off, made visible: a slip carrying the one line the next
      // worker is given. It is the whole collaboration model in one prop.
      const handsTo = state.chain.find((s) => s.step === payload.step + 1);
      if (handsTo) {
        push(MS.slip, () => {
          spotlightOff();
          slipFrom(payload.agent || agent, handsTo.agent, report, MS.slip);
        });
      }
      break;
    }

    case "run_finished":
    case "run_failed":
      push(MS.end, () => {
        spotlightOff();
        for (const li of document.querySelectorAll("#chain li.active")) {
          li.classList.remove("active");
        }
        if ((payload.summary || {}).ok) walk("marco", "out", MS.end * 2);
      });
      push(MS.end, () => showEndCard(payload.summary || {}, payload.reason));
      break;
  }
  return out;
}

function showPizza(lines) {
  const group = $("pizza-toppings");
  group.innerHTML = "";
  const ingredients = (lines[0] || {}).ingredients || [];
  // Dots for colour, not for accuracy: the real ingredient list is in the
  // rail, where it can be read. Deciding what a pizza "really" looks like
  // from an ingredient list would be game logic, and that lives in Python.
  ingredients.slice(0, 7).forEach((_, i) => {
    const angle = (i / Math.min(7, ingredients.length)) * Math.PI * 2;
    group.appendChild(svgEl("circle", {
      class: "topping-dot " + ["", "green", "pale"][i % 3],
      cx: 1342 + Math.cos(angle) * 28,
      cy: 648 + Math.sin(angle) * 8,
      r: 6,
    }));
  });
  gsap.fromTo($("plate-served"), { autoAlpha: 0, y: -28 },
              { autoAlpha: 1, y: 0, duration: secs(700), ease: "bounce.out" });
}

/* ------------------------------------------------------------------ *
 * Layer 3: the pacer
 * ------------------------------------------------------------------ */

const queue = [];
let draining = false;

function enqueue(event) {
  reduce(event);
  queue.push(...beats(event));
  drain();
}

async function drain() {
  if (draining) return;
  draining = true;
  while (queue.length) {
    const beat = queue.shift();
    try { beat.run(); } catch (err) { console.error(err); }
    await sleep(beat.ms / speed());
  }
  draining = false;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const speed = () => Number($("speed").value) || 1;

/* ------------------------------------------------------------------ *
 * The stage, reset
 * ------------------------------------------------------------------ */

function resetStage() {
  gsap.killTweensOf("*");
  place("marco", "door");
  place("sophia", "till");
  place("chef", "prep");
  for (const name of AGENTS) {
    const el = actor(name);
    gsap.set(el, { rotation: 0 });
    gsap.set(el.querySelectorAll(".arm"), { rotation: 0 });
    gsap.set(el.querySelector(".head"), { rotation: 0 });
    gsap.set(el.querySelector(".body"), { scaleX: 1, scaleY: 1 });
    gsap.set(el.querySelector(".bubble"), { autoAlpha: 0 });
  }
  spotlightOff();
  gsap.set([$("plate-served"), $("slip")], { autoAlpha: 0 });
  $("board").classList.remove("lit");
  $("oven-glow").setAttribute("opacity", "0");
  $("till-total").textContent = "";
  setPhase("DOOR");
}

function resetRun() {
  queue.length = 0;
  state.cost = 0;
  state.transcript = [];
  state.lastSummary = null;
  $("cost-chip").textContent = "";
  $("transcript").innerHTML =
    '<p class="empty">Nothing yet. Place an order and the chain runs.</p>';
  for (const step of state.chain) {
    $("chain-" + step.step)?.classList.remove("active", "done");
  }
  resetStage();
}

function drawTiles() {
  const tiles = $("tiles");
  for (let x = 0; x <= 1920; x += 160) {
    tiles.appendChild(svgEl("line", { x1: x, y1: 560, x2: x - 110, y2: 940 }));
  }
  for (let y = 610; y <= 940; y += 80) {
    tiles.appendChild(svgEl("line", { x1: 0, y1: y, x2: 1920, y2: y }));
  }
}

/* ------------------------------------------------------------------ *
 * The menu: a board on the wall, and an order you can click together
 * ------------------------------------------------------------------ */

const builder = { pizza: null, extras: new Set(), remove: new Set() };

async function loadMenu() {
  try {
    state.menu = await (await fetch("/api/menu")).json();
  } catch {
    return;   // the board stays blank; it is scenery, never a dependency
  }
  paintBoard(state.menu);
  paintMenuCard(state.menu);
}

function paintBoard(menu) {
  const rows = $("board-rows");
  rows.innerHTML = "";
  // A tight centred block, not a name pinned left and a price pinned right.
  // The price used to be anchored at x=768 while the board itself ends at
  // x=760, so it hung off the edge with a hand's width of nothing before it.
  Object.entries(menu.pizzas).forEach(([name, spec], i) => {
    const y = 200 + i * 46;
    rows.appendChild(svgEl("text", { class: "board-item", x: 452, y }, titleCase(name)));
    rows.appendChild(svgEl("text", { class: "board-price", x: 648, y }, spec.price));
  });
  const no = (menu.not_served || []).slice(0, 2).join(" or ");
  $("board-note").textContent = no ? `pizza only — no ${no}` : "";
}

function paintMenuCard(menu) {
  const body = $("menu-body");
  body.innerHTML = "";

  const pizzas = document.createElement("div");
  for (const [name, spec] of Object.entries(menu.pizzas)) {
    const row = document.createElement("button");
    row.className = "menu-row";
    row.dataset.pizza = name;
    row.innerHTML = `<span>
        <span class="nm">${escapeHtml(titleCase(name))}</span><br>
        <span class="made">${escapeHtml(spec.base.join(", "))}</span>
      </span><span class="pr">${escapeHtml(String(spec.price))}</span>`;
    row.addEventListener("click", () => choosePizza(name));
    pizzas.appendChild(row);
  }
  body.appendChild(pizzas);

  body.appendChild(heading("Add a topping"));
  const extras = document.createElement("div");
  extras.className = "chips";
  for (const [name, price] of Object.entries(menu.toppings)) {
    const chip = document.createElement("button");
    chip.className = "chip-btn";
    chip.dataset.extra = name;
    chip.textContent = `${titleCase(name)} +${price}`;
    chip.addEventListener("click", () => toggle(builder.extras, name));
    extras.appendChild(chip);
  }
  body.appendChild(extras);

  body.appendChild(heading("Leave something off"));
  const removes = document.createElement("div");
  removes.className = "chips";
  removes.id = "remove-chips";
  body.appendChild(removes);

  body.appendChild(heading("We do not serve"));
  const no = document.createElement("div");
  no.className = "chips";
  for (const name of menu.not_served || []) {
    const chip = document.createElement("span");
    chip.className = "chip-btn no";
    chip.textContent = titleCase(name);
    no.appendChild(chip);
  }
  body.appendChild(no);

  $("menu-sub").textContent =
    `${Object.keys(menu.pizzas).length} pizzas, ${Object.keys(menu.toppings).length} toppings`
    + ` — tax ${Number(menu.tax_rate) * 100}%, at most ${menu.max_items} items`
    + " · served from shop/menu.json, the file the sensors price against";

  choosePizza(Object.keys(menu.pizzas)[0]);
}

function heading(text) {
  const h = document.createElement("p");
  h.className = "h";
  h.textContent = text.toUpperCase();
  return h;
}

function choosePizza(name) {
  builder.pizza = name;
  builder.remove.clear();
  const chips = $("remove-chips");
  chips.innerHTML = "";
  for (const ingredient of state.menu.pizzas[name].base) {
    const chip = document.createElement("button");
    chip.className = "chip-btn";
    chip.dataset.remove = ingredient;
    chip.textContent = titleCase(ingredient);
    chip.addEventListener("click", () => toggle(builder.remove, ingredient));
    chips.appendChild(chip);
  }
  paintBuilder();
}

function toggle(set, value) {
  set.has(value) ? set.delete(value) : set.add(value);
  paintBuilder();
}

function paintBuilder() {
  for (const row of document.querySelectorAll("[data-pizza]")) {
    row.classList.toggle("on", row.dataset.pizza === builder.pizza);
  }
  for (const chip of document.querySelectorAll("[data-extra]")) {
    chip.classList.toggle("on", builder.extras.has(chip.dataset.extra));
  }
  for (const chip of document.querySelectorAll("[data-remove]")) {
    chip.classList.toggle("on", builder.remove.has(chip.dataset.remove));
  }
  $("say-line").textContent = builderSentence();
}

/** What you would say out loud.
 *
 * A sentence, deliberately - not a structured order. Marco still has to read
 * the menu and call place_order himself, and his sensor grades whether he got
 * it right. Posting a finished order here would delete his whole job.
 */
function builderSentence() {
  if (!builder.pizza) return "";
  const parts = [`one ${builder.pizza}`];
  if (builder.remove.size) parts.push("no " + [...builder.remove].join(" or "));
  if (builder.extras.size) parts.push("with " + [...builder.extras].join(" and "));
  return parts.join(", ");
}

/* ------------------------------------------------------------------ *
 * The end card
 * ------------------------------------------------------------------ */

function showEndCard(summary, reason) {
  const ok = Boolean(summary.ok);
  $("end-title").textContent = ok ? "SHIFT COMPLETE" : "SHIFT FAILED";
  $("end-title").style.color = ok ? "var(--ok)" : "var(--bad)";
  $("end-line").textContent = reason
    || `ended ${summary.phase} · "${summary.order_text || ""}"`;

  const chain = $("end-chain");
  chain.innerHTML = "";
  for (const step of summary.steps || []) {
    const div = document.createElement("div");
    div.className = "line";
    div.innerHTML = `<span class="who ${step.agent}-ink">${escapeHtml(step.agent)}
      <span style="opacity:.6">${escapeHtml(step.job)}</span></span>
      <span class="what">${escapeHtml(step.report || "-")}</span>`;
    chain.appendChild(div);
  }

  const steps = summary.steps || [];
  const rows = steps.map((s) => `
    <tr><td>${escapeHtml(s.agent)}</td>
        <td>${escapeHtml(shortModel(state.models[s.agent]))}</td>
        <td class="num">${s.turns || "-"}</td>
        <td class="num">${s.tool_calls}</td>
        <td class="num">${(s.in_tokens || 0).toLocaleString()}</td>
        <td class="num">${(s.out_tokens || 0).toLocaleString()}</td>
        <td class="num">$${Number(s.cost_usd || 0).toFixed(4)}</td>
        <td class="num">${((s.wall_ms || 0) / 1000).toFixed(1)}s</td></tr>`).join("");
  $("end-table").innerHTML = `
    <tr><th>step</th><th>model</th><th class="num">turns</th>
        <th class="num">tools</th><th class="num">in</th>
        <th class="num">out</th><th class="num">$</th><th class="num">time</th></tr>
    ${rows}`;

  // The line that explains the input column. A step pays for its whole
  // context on every turn and almost all of it comes back as a cache read,
  // which is why a shift moves 160,000 tokens and costs fifteen cents.
  const sum = (key) => steps.reduce((n, s) => n + Number(s[key] || 0), 0);
  const inTotal = sum("in_tokens");
  const cached = sum("in_cache_read") + sum("in_cache_write");
  const price = `$${Number(summary.cost_usd || 0).toFixed(4)}`;
  const log = `<span class="dim">runs/${escapeHtml(summary.run_id)}.jsonl</span>`;
  $("end-totals").innerHTML = cached
    ? `${price} · ${inTotal.toLocaleString()} input tokens over ${sum("turns")} `
      + `turns, ${Math.round(100 * cached / inTotal)}% of it context re-sent `
      + `from cache<br>${log}`
    : `${price} · ${log}`;

  $("endcard").classList.remove("hidden");
  setRunning(false);
}

/* ------------------------------------------------------------------ *
 * Talking to the server
 * ------------------------------------------------------------------ */

function connect(runId) {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.onmessage = (message) => {
    if (message.data === "done") { source.close(); return; }
    try { enqueue(JSON.parse(message.data)); } catch (err) { console.error(err); }
  };
  source.onerror = () => source.close();
}

async function startRun() {
  const order = $("order").value.trim();
  if (!order) return;
  resetRun();
  setRunning(true);

  let response;
  try {
    response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_text: order, budget_usd: 0.9 }),
    });
  } catch {
    setRunning(false);
    return say("marco", "the shop is not answering - is the server running?", "fail");
  }
  if (!response.ok) {
    setRunning(false);
    return say("marco", await response.text(), "fail");
  }
  const { run_id } = await response.json();
  state.runId = run_id;
  connect(run_id);
}

function setRunning(running) {
  state.live = running;
  $("start").disabled = running;
  $("past").disabled = running;
  $("start").textContent = running ? "ON SHIFT…" : "WALK IN";
}

async function loadPastRuns() {
  try {
    const runs = await (await fetch("/api/runs")).json();
    const select = $("past");
    select.length = 1;
    for (const run of runs) {
      const option = document.createElement("option");
      option.value = run.run_id;
      option.textContent = `${run.run_id} · ${run.phase || "?"} · `
        + `"${(run.order_text || "").slice(0, 28)}"`;
      select.appendChild(option);
    }
  } catch { /* the dropdown stays empty */ }
}

async function loadChain() {
  try {
    const data = await (await fetch("/api/chain")).json();
    houseCast = data.cast || houseCast;
    state.models = { ...houseCast };
    state.chain = data.chain || [];
  } catch {
    state.chain = [
      { step: 1, agent: "marco", job: "order" },
      { step: 2, agent: "sophia", job: "ring_up" },
      { step: 3, agent: "chef", job: "cook" },
      { step: 4, agent: "marco", job: "pickup" },
    ];
  }
  syncPlates();
  renderChain();
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

$("start").addEventListener("click", startRun);
$("order").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !state.live) startRun();
});

$("menu-btn").addEventListener("click", openMenu);
$("build").addEventListener("click", openMenu);
function openMenu() {
  if (!state.menu) loadMenu();
  $("menucard").classList.remove("hidden");
}
$("menu-close").addEventListener("click", () => $("menucard").classList.add("hidden"));
$("menu-use").addEventListener("click", () => {
  const sentence = builderSentence();
  if (sentence) $("order").value = sentence;
  $("menucard").classList.add("hidden");
  $("order").focus();
});

$("past").addEventListener("change", (e) => {
  if (!e.target.value) return;
  resetRun();
  setRunning(true);
  state.runId = e.target.value;
  connect(e.target.value);
});

$("end-new").addEventListener("click", () => {
  $("endcard").classList.add("hidden");
  resetRun();
});
$("end-replay").addEventListener("click", () => {
  $("endcard").classList.add("hidden");
  if (!state.runId) return;
  resetRun();
  setRunning(true);
  connect(state.runId);
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  $("menucard").classList.add("hidden");
  $("endcard").classList.add("hidden");
});

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  root.setAttribute("data-theme",
    root.getAttribute("data-theme") === "dark" ? "light" : "dark");
});

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

drawTiles();
for (const el of document.querySelectorAll(".bubble, #slip, #plate-served")) {
  el.classList.remove("hidden");   // display:none would defeat autoAlpha
}
resetStage();
loadChain();
loadMenu();
loadPastRuns();

const params = new URLSearchParams(location.search);
if (params.get("theme")) {
  document.documentElement.setAttribute("data-theme", params.get("theme"));
}
if (params.get("run")) {
  setRunning(true);
  state.runId = params.get("run");
  connect(params.get("run"));
}
