// Progress over time: each player's share of their locations checked, from the history the manager records
// (history.py), with the standings below it. The table is also the chart's text alternative.
import { h, clear, fmtDate, fmtSpan } from "./util.js";
import { t, locale } from "./i18n.js";

const SVG = "http://www.w3.org/2000/svg";
const HEIGHT = 240;
const MARGIN = { top: 10, right: 14, bottom: 26, left: 42 };
const COLORED = 8;  // more players than series colors: the viewer's own slots in color, the rest in one gray
const TIME_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000];

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) if (value != null) el.setAttribute(key, value);
  el.append(...children);
  return el;
}

// Ticks on round times of the viewer's clock, about every 90 px
function timeTicks(start, end, width) {
  const span = end - start;
  const step = TIME_STEPS.find((s) => span / s <= Math.max(2, Math.floor(width / 90))) || TIME_STEPS.at(-1);
  const offset = new Date(start * 1000).getTimezoneOffset() * 60;
  const ticks = [];
  for (let tick = Math.ceil((start - offset) / step) * step + offset; tick <= end; tick += step) ticks.push(tick);
  const fmt = step >= 86400 ? { day: "numeric", month: "short" }
    : span > 86400 ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
  return ticks.map((tick) => [tick, new Date(tick * 1000).toLocaleString(locale, fmt)]);
}

// The last value at or before `at` of a step series [[time, value], ...]; null before the first point
function valueAt(points, at) {
  let value = null;
  for (const [time, v] of points) {
    if (time > at) break;
    value = v;
  }
  return value;
}

// A race time to the second: "1:23:45", or "23:45" under an hour
export function raceTime(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60, secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

export function progressSection(data, { mine = new Set(), cleanup }) {
  // a race counts from its planned start; any other game from its first check
  const start = data.race_start ?? data.play_started_at ?? data.recorded_from;
  const end = Math.max(data.finished_at ?? data.now, start + 60);
  const known = (ts) => ts != null && ts > data.recorded_from + 1;  // goals before recording began have no real time
  const players = data.slots.filter((s) => s.points.length).map((s, i, all) => {
    const pct = s.points.map(([time, checked]) => [time, s.total ? (100 * checked) / s.total : 0]);
    const color = all.length <= COLORED ? `var(--series-${i + 1})` : mine.has(s.slot) ? "var(--series-1)" : "var(--series-rest)";
    return { ...s, pct, color, checked: s.points.at(-1)[1] };
  });
  const times = [...new Set([start, end, ...players.flatMap((p) => p.points.map(([time]) => time))])]
    .filter((time) => time >= start && time <= end).sort((a, b) => a - b);
  let focused = null, cursor = null, width = 0;

  const frame = h("div", { class: "chart" });
  const tip = h("div", { class: "chart-tip", hidden: true, role: "status" });
  const legend = h("div", { class: "legend", role: "group", "aria-label": t("Players in the chart") });
  const plot = svg("svg", { height: HEIGHT, role: "img", tabindex: 0,
    "aria-label": t("Share of locations checked over time, per player. The table below has the same numbers.") });
  frame.append(plot, tip);

  const x = (time) => MARGIN.left + ((time - start) / (end - start)) * (width - MARGIN.left - MARGIN.right);
  const y = (pct) => MARGIN.top + (1 - pct / 100) * (HEIGHT - MARGIN.top - MARGIN.bottom);

  function path(p) {
    const first = valueAt(p.pct, start);
    let d = first == null ? `M${x(p.pct[0][0])},${y(p.pct[0][1])}` : `M${x(start)},${y(first)}`;
    for (const [time, pct] of p.pct) if (time > start) d += `H${x(time)}V${y(pct)}`;
    return d + `H${x(end)}`;
  }

  function draw() {
    width = frame.clientWidth;
    if (!width) return;
    plot.setAttribute("viewBox", `0 0 ${width} ${HEIGHT}`);
    plot.setAttribute("width", width);
    const children = [];
    for (const pct of [0, 25, 50, 75, 100]) {
      children.push(svg("line", { class: "gridline", x1: MARGIN.left, x2: width - MARGIN.right, y1: y(pct), y2: y(pct) }),
        svg("text", { class: "axis-label", x: MARGIN.left - 8, y: y(pct) + 4, "text-anchor": "end" }, `${pct}%`));
    }
    for (const [tick, label] of timeTicks(start, end, width - MARGIN.left - MARGIN.right)) {
      children.push(svg("text", { class: "axis-label", x: x(tick), y: HEIGHT - 6, "text-anchor": "middle" }, label));
    }
    // the focused player on top, and the viewer's own above the rest
    const order = [...players].sort((a, b) => (a.slot === focused) - (b.slot === focused) || mine.has(a.slot) - mine.has(b.slot));
    for (const p of order) {
      const dim = focused != null && p.slot !== focused;
      const color = p.slot === focused && p.color === "var(--series-rest)" ? "var(--series-2)" : p.color;
      children.push(svg("path", { class: `series${dim ? " dim" : ""}`, d: path(p), stroke: color }));
      if (known(p.goal_at) && p.goal_at <= end) {
        children.push(svg("circle", { class: `marker${dim ? " dim" : ""}`, cx: x(p.goal_at), cy: y(valueAt(p.pct, p.goal_at) ?? 0), r: 5, fill: color },
          svg("title", {}, t("{player} reached their goal", { player: p.name }))));
      }
    }
    if (cursor != null) children.push(svg("line", { class: "crosshair", x1: x(times[cursor]), x2: x(times[cursor]), y1: MARGIN.top, y2: HEIGHT - MARGIN.bottom }));
    plot.replaceChildren(...children);
    drawTip();
  }

  function drawTip() {
    if (cursor == null) { tip.hidden = true; return; }
    const at = times[cursor];
    const rows = players.map((p) => ({ p, pct: valueAt(p.pct, at), checked: valueAt(p.points, at) }))
      .filter((r) => r.pct != null).sort((a, b) => b.pct - a.pct);
    const shown = rows.slice(0, 12);
    clear(tip).append(h("div", { class: "when" }, fmtDate(at)),
      shown.map(({ p, pct, checked }) => h("div", { class: "row-line" },
        h("span", { class: "line-key", style: { background: p.color } }),
        h("span", { class: "value" }, `${Math.round(pct)}%`),
        h("span", { class: "name" }, p.name, ` · ${checked}/${p.total}`, p.goal_at && p.goal_at <= at ? ` · ${t("goal")}` : ""))),
      rows.length > shown.length ? h("div", { class: "name" }, t("…and {n} more", { n: rows.length - shown.length })) : null);
    tip.hidden = false;
    const left = x(at);
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${left + 14 + tipWidth > width ? Math.max(0, left - 14 - tipWidth) : left + 14}px`;
  }

  function point(event) {
    const box = plot.getBoundingClientRect();
    const at = start + ((event.clientX - box.left - MARGIN.left) / (width - MARGIN.left - MARGIN.right)) * (end - start);
    let best = 0;
    times.forEach((time, i) => { if (Math.abs(time - at) < Math.abs(times[best] - at)) best = i; });
    if (best !== cursor) { cursor = best; draw(); }
  }
  plot.addEventListener("pointermove", point);
  plot.addEventListener("pointerdown", point);
  plot.addEventListener("pointerleave", () => { cursor = null; draw(); });
  plot.addEventListener("blur", () => { cursor = null; draw(); });
  plot.addEventListener("keydown", (e) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (e.key === "Escape") { cursor = null; draw(); return; }
    if (!(e.key in moves)) return;
    e.preventDefault();
    cursor = Math.min(times.length - 1, Math.max(0, (cursor ?? times.length - 1) + moves[e.key]));
    draw();
  });

  // The legend names every line; pressing a name brings that player to the front
  const buttons = players.map((p) => {
    const button = h("button", { type: "button", "aria-pressed": "false" }, h("span", { class: "line-key", style: { background: p.color } }), p.name);
    button.onclick = () => {
      focused = focused === p.slot ? null : p.slot;
      for (const [other, b] of buttons) b.setAttribute("aria-pressed", String(other.slot === focused));
      draw();
    };
    return [p, button];
  });
  legend.append(buttons.map(([, b]) => b));

  const observer = new ResizeObserver(() => { if (frame.clientWidth !== width) draw(); });
  observer.observe(frame);
  cleanup(() => observer.disconnect());
  requestAnimationFrame(draw);

  return h("div", { class: "stack" }, players.length ? [frame, players.length > 1 ? legend : null] : null, standings(data, players, known, start));
}

// Who reached their goal first, then who got furthest
function standings(data, players, known, start) {
  const rows = data.slots.map((s) => {
    const p = players.find((q) => q.slot === s.slot);
    const checked = p ? p.checked : 0;
    return { ...s, checked, pct: s.total ? checked / s.total : 0, color: p?.color };
  }).sort((a, b) => (a.goal_at == null) - (b.goal_at == null) || (a.goal_at ?? 0) - (b.goal_at ?? 0) || b.pct - a.pct);
  const goal = (s) => {
    if (s.goal_at == null) return h("span", { class: "muted" }, "—");
    if (!known(s.goal_at)) return h("span", { class: "muted" }, t("before recording began"));
    if (data.race_start != null && s.goal_at >= data.race_start) {
      return [h("strong", { class: "mono" }, raceTime(s.goal_at - data.race_start)), h("div", { class: "muted small" }, fmtDate(s.goal_at))];
    }
    return [fmtDate(s.goal_at), start != null && s.goal_at >= start ? h("div", { class: "muted small" }, t("after {time}", { time: fmtSpan(s.goal_at - start) })) : null];
  };
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["#", t("Player"), t("Game"), t("Checked"), data.race_start != null ? t("Race time") : t("Goal reached")].map((text) => h("th", {}, text)))),
    h("tbody", {}, rows.map((s, i) => h("tr", {},
      h("td", { class: "muted" }, s.goal_at != null ? String(i + 1) : ""),
      h("td", {}, h("span", { class: "row", style: { gap: "8px" } }, s.color ? h("span", { class: "line-key", style: { background: s.color } }) : null,
        h("span", { class: "slot-name" }, s.name)), s.owner ? h("div", { class: "muted small" }, s.owner) : null),
      h("td", {}, s.game),
      h("td", {}, t("{checked} of {total}", { checked: s.checked, total: s.total }), h("span", { class: "muted" }, ` · ${Math.round(100 * s.pct)}%`)),
      h("td", {}, goal(s)))))));
}

// One line about when the game was played
export function playedText(data) {
  const begun = data.race_start ?? data.play_started_at;
  const lastGoal = Math.max(...data.slots.map((s) => s.goal_at ?? 0));
  if (data.finished_at && data.race_start != null && lastGoal >= data.race_start) {
    return t("Race finished {date}, the last player after {time}", { date: fmtDate(lastGoal), time: raceTime(lastGoal - data.race_start) });
  }
  if (data.finished_at) {
    return begun
      ? t("Finished {date} after {time}", { date: fmtDate(data.finished_at), time: fmtSpan(data.finished_at - begun) })
      : t("Finished {date}", { date: fmtDate(data.finished_at) });
  }
  if (data.race_start != null) return t("Race started {date}, {time} ago", { date: fmtDate(data.race_start), time: fmtSpan(data.now - data.race_start) });
  if (begun) return t("Started {date}, {time} ago", { date: fmtDate(begun), time: fmtSpan(data.now - begun) });
  if (data.recorded_from) return t("Recorded since {date}", { date: fmtDate(data.recorded_from) });
  return null;
}
