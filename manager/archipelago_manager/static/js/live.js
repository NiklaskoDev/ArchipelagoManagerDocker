// Pages that show things that change (games, sign-ups, a generation, the activity of a game) ask the server for news
// every few seconds. They all do it through every() here, so that it is out in the open: a line above the page says when
// it last updated, with a button that updates it now and one that pauses the automatic updates, a choice kept in this
// browser. A page in a background tab does not ask, and catches up as soon as it is in view again. Leaving the page
// (or signing out) ends its updates (the router's cleanup).
//
// Not here: the text client and the server console, which the user opens and closes themselves, and clocks that only
// count down on the page.
import { h, icon, fmtAgo } from "./util.js";
import { t, plural } from "./i18n.js";

const KEY = "apm-auto-refresh";
const jobs = new Set();
let paused = (() => { try { return localStorage.getItem(KEY) === "off"; } catch { return false; } })();
let lastAt = Date.now(), failed = false, busy = 0;
let control = null, clock = null;
// The pause applies where its buttons are: in the app (liveControl). The spectator page (watch.js) shows a game's
// activity without them, so it keeps updating, also in a browser that paused the app's updates.
let pausable = false;
const isPaused = () => paused && pausable;

// Calls fn every `ms` milliseconds while the page is shown and in view, until the page's cleanup. fn may throw: the
// line above the page then says that the update failed, and the next one tries again. With hiddenMs, it also runs that often
// while the tab is in the background (the game page counts news for the tab's title that way). Returns a function
// that ends the updates before the page's cleanup does.
export function every(ms, fn, cleanup, { hiddenMs = 0 } = {}) {
  const job = { ms, fn, hiddenMs, timer: null, last: Date.now(), running: false };
  jobs.add(job);
  lastAt = Date.now();  // the page has just loaded what it shows
  schedule(job);
  const stop = () => {
    jobs.delete(job);
    clearTimeout(job.timer);
    drawControl();
  };
  cleanup(stop);
  drawControl();
  return stop;
}

function schedule(job) {
  clearTimeout(job.timer);
  if (jobs.has(job) && !isPaused()) job.timer = setTimeout(() => tick(job), job.ms);
}

async function tick(job) {
  if (!document.hidden || (job.hiddenMs && Date.now() - job.last >= job.hiddenMs)) await run(job);
  schedule(job);
}

async function run(job) {
  if (job.running || !jobs.has(job)) return;
  job.running = true;
  job.last = Date.now();
  busy++;
  drawControl();
  try {
    await job.fn();
    lastAt = Date.now();
    failed = false;
  } catch {
    failed = true;
  } finally {
    job.running = false;
    busy--;
    drawControl();
  }
}

// Everything on the page, now; the automatic updates count again from here
export async function refreshNow() {
  await Promise.all([...jobs].map(async (job) => {
    await run(job);
    schedule(job);
  }));
}

function setPaused(value) {
  paused = value;
  try {
    if (paused) localStorage.setItem(KEY, "off");
    else localStorage.removeItem(KEY);
  } catch { /* private mode: the choice lasts until reload only */ }
  if (paused) jobs.forEach((job) => clearTimeout(job.timer));
  else refreshNow();
  drawControl();
}

// Back in view after a while: what is due is fetched at once instead of at the next turn
document.addEventListener("visibilitychange", () => {
  if (document.hidden || isPaused()) return;
  for (const job of jobs) {
    if (Date.now() - job.last >= job.ms) run(job).then(() => schedule(job));
  }
  drawControl();
});

// The browser's other tabs of this server follow a choice made in one of them
window.addEventListener("storage", (e) => {
  if (e.key !== KEY && e.key !== null) return;
  if ((e.newValue === "off") !== paused) setPaused(e.newValue === "off");
});

// ---------------------------------------------------------------------------------------------------------------------
// a line above the page (app.js), hidden on pages that do not update themselves
// ---------------------------------------------------------------------------------------------------------------------
export function liveControl() {
  pausable = true;
  const text = h("span", { class: "live-text" });
  const refresh = h("button", { class: "btn ghost sm live-refresh", type: "button", onclick: () => refreshNow() }, icon("restart", 15), text);
  const toggle = h("button", { class: "btn ghost sm icon-only live-toggle", type: "button", onclick: () => setPaused(!paused) });
  control = { root: h("div", { class: "live", role: "group", hidden: true }, refresh, toggle), refresh, toggle, text };
  drawControl();
  return control.root;
}

function ago() {
  const s = Math.round((Date.now() - lastAt) / 1000);
  if (s < 5) return t("Updated just now");
  return s < 60 ? t("Updated {n} s ago", { n: s }) : t("Updated {ago}", { ago: fmtAgo(lastAt / 1000) });
}

function drawControl() {
  if (!control?.root.isConnected) {
    clearInterval(clock);
    clock = null;
    return;
  }
  const { root, refresh, toggle, text } = control;
  root.hidden = !jobs.size;
  if (!jobs.size) {
    clearInterval(clock);
    clock = null;
    return;
  }
  const seconds = Math.round(Math.min(...[...jobs].map((job) => job.ms)) / 1000) || 1;
  root.setAttribute("aria-label", t("Automatic updates"));
  root.classList.toggle("busy", busy > 0);
  root.classList.toggle("paused", paused);
  root.classList.toggle("failed", failed && !paused);
  text.textContent = paused ? t("Updates paused") : failed ? t("Update failed") : ago();
  const about = paused ? t("Automatic updates are paused: this page only changes when you refresh it.")
    : plural(seconds, "This page updates itself every second while it is in view.", "This page updates itself every {n} seconds while it is in view.");
  refresh.title = `${t("Refresh now")} · ${about}`;
  refresh.setAttribute("aria-label", `${t("Refresh now")}. ${text.textContent}`);
  if (toggle.dataset.paused !== String(paused)) {
    toggle.dataset.paused = String(paused);
    toggle.replaceChildren(icon(paused ? "play" : "pause", 15));
  }
  toggle.title = paused ? t("Resume automatic updates") : t("Pause automatic updates");
  toggle.setAttribute("aria-label", toggle.title);
  toggle.setAttribute("aria-pressed", String(paused));
  // "updated 12 s ago" counts on by itself, while it can be seen
  if (!clock) clock = setInterval(() => { if (!document.hidden) drawControl(); }, 1000);
}
