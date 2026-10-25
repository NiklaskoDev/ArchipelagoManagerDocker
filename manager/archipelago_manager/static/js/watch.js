// The spectator page (watch.html#<link>) and its overlay for streams (watch.html#<link>/overlay): a game as spectators
// see it, without signing in (spectate.py on the server). It has no address, password or user names, only the slots.
import { h, clear, fmtUntil } from "./util.js";
import { get, ApiError } from "./api.js";
import { stateBadge, badge, progressBar, banner } from "./components.js";
import { progressSection, playedText, raceTime } from "./progresschart.js";
import { activitySection } from "./feed.js";
import { t } from "./i18n.js";

const root = document.getElementById("watch");
const [token = "", mode] = decodeURIComponent(location.hash.slice(1)).split("/");
const overlay = mode === "overlay";
document.body.classList.toggle("overlay", overlay);
const SAID = { stuck: ["warn", () => t("Stuck")], away: ["muted", () => t("On a break")] };

let data = null;
let offset = 0;  // the server's clock minus this one's, so the race clock agrees with the server
const now = () => Date.now() / 1000 + offset;

// Who reached their goal first, then who got furthest, as in the standings of the game page
function ranked(players) {
  const pct = (p) => (p.total ? p.checked / p.total : 0);
  return [...players].sort((a, b) => (a.goal_at == null) - (b.goal_at == null) || (a.goal_at ?? 0) - (b.goal_at ?? 0) || pct(b) - pct(a));
}

const raceStart = () => data.history.race_start;

// What the clock shows: the countdown to a planned start, the time since a race started, or nothing
function clockText() {
  const start = data.start;
  if (start && start.stage !== "started" && start.at > now()) {
    const left = Math.round(start.at - now());
    return { label: t("Starts in"), value: left < 3600 ? raceTime(left) : fmtUntil(start.at) };
  }
  if (raceStart() != null && !data.finished_at) return { label: t("Race time"), value: raceTime(now() - raceStart()) };
  return null;
}

// What a player's row says at its end: their race time or goal, else how far they got
function result(p) {
  if (p.goal_at != null) {
    return raceStart() != null && p.goal_at >= raceStart() ? raceTime(p.goal_at - raceStart()) : t("Goal");
  }
  return p.total ? `${Math.floor((100 * p.checked) / p.total)}%` : "—";
}

// ---------------------------------------------------------------------------------------------------------------------
// the overlay: one compact panel for a browser source in OBS
// ---------------------------------------------------------------------------------------------------------------------
const clockValue = h("span", { class: "race-clock" });
const clockLabel = h("span", { class: "muted small" });
function tick() {
  const clock = data && clockText();
  clockValue.textContent = clock ? clock.value : "";
  clockLabel.textContent = clock ? clock.label : "";
}

function drawOverlay() {
  clear(root).append(h("div", { class: "ov" },
    h("div", { class: "ov-head" }, h("span", { class: "ov-name" }, data.name), h("span", { class: "ov-clock" }, clockLabel, clockValue)),
    ranked(data.players).map((p, i) => h("div", { class: `ov-row${p.goal_at != null ? " done" : ""}` },
      h("span", { class: "ov-rank" }, p.goal_at != null ? String(i + 1) : ""),
      h("span", { class: "ov-player" }, h("span", { class: `dot ${p.status === "online" || p.status === "goal" ? "ok" : ""}` }), h("span", { class: "ov-player-name" }, p.name)),
      h("div", { class: "progress" }, h("div", { class: `bar ${p.goal_at != null ? "done" : ""}`, style: { width: `${p.total ? Math.min(100, (100 * p.checked) / p.total) : 0}%` } })),
      h("span", { class: "ov-value" }, result(p))))));
  tick();
}

// ---------------------------------------------------------------------------------------------------------------------
// the page for viewers
// ---------------------------------------------------------------------------------------------------------------------
const headHost = h("div");
const playersHost = h("div", { class: "section" });
const chartHost = h("div");
const activityHost = h("div");
let chartSignature = "", chartStops = [];

function statusCell(p) {
  if (p.goal_at != null) return badge(t("Goal complete"), "ok");
  const said = SAID[p.player_status];
  return h("span", { class: "row", style: { gap: "8px" } },
    h("span", { class: "status-line" }, h("span", { class: `dot ${p.status === "online" ? "ok" : ""}` }), p.status === "online" ? t("Online") : t("Offline")),
    said ? badge(said[1](), said[0]) : null);
}

function drawPage() {
  document.title = `${data.name} · Archipelago`;
  const played = data.history.recorded_from ? playedText(data.history) : null;
  clear(headHost).append(h("div", { class: "page-head" },
    h("div", {},
      h("div", { class: "row" }, h("h1", {}, data.name), stateBadge(data.state), data.race_mode ? badge(t("Race mode"), "warn") : null,
        data.finished_at ? badge(t("Finished"), "ok") : null),
      played ? h("p", { class: "muted" }, played) : null),
    h("div", { class: "watch-clock" }, clockLabel, clockValue)));
  tick();
  const goal = raceStart() != null ? t("Race time") : t("Goal");
  clear(playersHost).append(h("div", { class: "section-head" }, h("h2", {}, t("Players"))),
    h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["#", t("Player"), t("Game"), t("Progress"), t("Status"), goal].map((text) => h("th", {}, text)))),
      h("tbody", {}, ranked(data.players).map((p, i) => h("tr", {},
        h("td", { class: "muted" }, p.goal_at != null ? String(i + 1) : ""),
        h("td", {}, h("span", { class: "slot-name" }, p.name)),
        h("td", {}, p.game),
        h("td", {}, progressBar(p.checked, p.total, { compact: true })),
        h("td", {}, statusCell(p)),
        h("td", { class: "mono" }, p.goal_at != null ? result(p) : h("span", { class: "muted" }, "—"))))))));
  drawChart();
}

function drawChart() {
  const { now: _now, ...rest } = data.history;
  const sig = JSON.stringify(rest);
  if (sig === chartSignature) return;
  chartSignature = sig;
  chartStops.splice(0).forEach((fn) => fn());
  clear(chartHost);
  if (!data.history.recorded_from) return;
  chartHost.append(h("div", { class: "section" }, h("div", { class: "card stack" },
    h("h2", {}, data.finished_at ? t("Results") : t("Progress over time")),
    progressSection(data.history, { cleanup: (fn) => chartStops.push(fn), table: false }))));
}

// ---------------------------------------------------------------------------------------------------------------------
let drawn = "", activityStops = null;
async function load() {
  try {
    data = await get(`/api/watch/${encodeURIComponent(token)}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      clear(root).append(h("div", { class: "auth-wrap" }, banner("warn", t("This link does not show a game (any more). Ask whoever shared it for a new one."))));
      return false;
    }
    if (!data) clear(root).append(h("div", { class: "auth-wrap" }, banner("error", err.message)));
    return true;  // try again later
  }
  offset = data.now - Date.now() / 1000;
  const { now: _now, history, ...rest } = data;
  const sig = JSON.stringify([rest, history.race_start, history.play_started_at]);
  if (overlay) {
    if (sig !== drawn) drawOverlay();
  } else {
    if (!root.contains(headHost)) {
      clear(root).append(h("main", { class: "view watch-page" }, headHost, playersHost, chartHost, activityHost,
        h("p", { class: "muted small center watch-foot" }, t("Spectator view of a game on Archipelago Manager"))));
    }
    if (sig !== drawn) drawPage();
    else drawChart();
    // the activity, while the game shows it to spectators
    if (data.activity && !activityStops) {
      const stops = activityStops = [];
      activityHost.append(activitySection({ mySlots: [], cleanup: (fn) => stops.push(fn), isCurrent: () => activityStops === stops,
        source: `/api/watch/${encodeURIComponent(token)}/activity`, spectator: true }));
    } else if (!data.activity && activityStops) {
      activityStops.splice(0).forEach((fn) => fn());
      activityStops = null;
      clear(activityHost);
    }
  }
  drawn = sig;
  return true;
}

if (!token) {
  clear(root).append(h("div", { class: "auth-wrap" }, banner("warn", t("This link does not show a game (any more). Ask whoever shared it for a new one."))));
} else if (await load()) {
  setInterval(tick, 1000);
  const timer = setInterval(async () => { if (!document.hidden && !(await load())) clearInterval(timer); }, 5000);
}
window.addEventListener("hashchange", () => location.reload());
