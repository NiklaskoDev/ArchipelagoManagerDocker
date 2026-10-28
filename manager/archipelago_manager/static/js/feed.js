// What happens in the multiworld, for everyone in the game: chat, items, hints, joins and goals (activity.py on the
// server, read from the server's log). Shown like the text client, in Archipelago's colors, newest at the bottom.
// Spectators (watch.js) get it from their link, without the chat.
import { h, clear, withNodes } from "./util.js";
import { get } from "./api.js";
import { t } from "./i18n.js";
import { checkbox } from "./components.js";

const KEEP = 500;  // entries on the page
const FILTERS = [
  ["all", () => t("All"), () => true],
  ["chat", () => t("Chat"), (e) => e.kind === "chat" || e.kind === "server"],
  ["item", () => t("Items"), (e) => e.kind === "item"],
  ["hint", () => t("Hints"), (e) => e.kind === "hint"],
  ["event", () => t("Joins and goals"), (e) => ["join", "part", "goal", "release", "collect", "status", "handover", "handover_done"].includes(e.kind)],
];
const HINT_STATUS = { found: () => t("found"), priority: () => t("priority"), avoid: () => t("avoid"),
  "no priority": () => t("no priority"), unspecified: () => t("unspecified") };

function clock(ts) {
  const d = new Date(ts * 1000);
  const today = d.toDateString() === new Date().toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// While the tab is in the background, onUnread(n) hears how many entries came in since that are news for the viewer
// (for the tab's title), and onUnread(0) once the tab is back in view.
export function activitySection({ gameId, mySlots, cleanup, isCurrent, source = `/api/games/${gameId}/activity`, spectator = false,
  onUnread = null }) {
  const mine = new Set(mySlots);
  const filters = spectator ? FILTERS.filter(([key]) => key !== "chat") : FILTERS;
  let entries = [], last = 0, filter = "all", timer = null, loaded = false, unread = 0;
  const list = h("div", { class: "console feed", role: "log", "aria-live": "polite", tabindex: 0 });
  const tabs = h("div", { class: "tabs", role: "tablist", style: { marginBottom: 0 } });
  const onlyMine = mine.size ? checkbox(t("Only about me"), false) : null;
  if (onlyMine) onlyMine.input.onchange = () => draw(true);

  const player = (name) => h("span", { class: mine.has(name) ? "tc-me" : "tc-player" }, name);
  const item = (name) => h("span", { class: "tc-item" }, name);
  const place = (name) => h("span", { class: "tc-location" }, name);
  const concerns = (e) => [e.player, e.finder, e.receiver].some((name) => mine.has(name));
  // news: what the others say, the items they send the viewer, and hints for items of theirs in the viewer's world
  const news = (e) => (e.kind === "chat" && !mine.has(e.player))
    || (e.kind === "item" && mine.has(e.receiver) && !mine.has(e.finder))
    || (e.kind === "hint" && mine.has(e.finder) && !mine.has(e.receiver));
  const user = (name) => h("strong", {}, name);

  function line(e) {
    let body;
    if (e.kind === "item") {
      body = e.finder === e.receiver
        ? withNodes(t("{finder} found their {item} ({location})"), { finder: player(e.finder), item: item(e.item), location: place(e.location) })
        : withNodes(t("{finder} sent {item} to {receiver} ({location})"),
          { finder: player(e.finder), item: item(e.item), receiver: player(e.receiver), location: place(e.location) });
    } else if (e.kind === "hint") {
      body = [withNodes(t("Hint: {receiver}'s {item} is at {location} in {finder}'s world"),
        { receiver: player(e.receiver), item: item(e.item), location: place(e.location), finder: player(e.finder) }),
      e.entrance ? [" ", h("span", { class: "tc-entrance" }, e.entrance)] : null,
      h("span", { class: "note" }, ` (${(HINT_STATUS[e.status] || (() => e.status))()})`)];
    } else if (e.kind === "chat") {
      body = [player(e.player), ": ", e.text];
    } else if (e.kind === "server") {
      body = h("span", { class: "note" }, t("Server: {text}", { text: e.text }));
    } else if (e.kind === "status") {  // what a player says about how they are doing
      const text = { playing: t("{player} is playing"), stuck: t("{player} is stuck"), away: t("{player} is taking a break") }[e.status]
        || t("{player} cleared their status");
      body = [withNodes(text, { player: player(e.player) }), e.note ? h("span", { class: "note" }, ` ${e.note}`) : null];
    } else if (e.kind === "handover") {  // a player hands their slot over (the names of users, not of slots)
      body = [withNodes(e.to ? t("{user} hands {player} over to {to}") : t("{user} is looking for someone to take over {player}"),
        { user: user(e.from), player: player(e.player), to: user(e.to || "") }), e.note ? h("span", { class: "note" }, ` ${e.note}`) : null];
    } else if (e.kind === "handover_done") {
      body = withNodes(e.from ? t("{to} takes over {player} from {user}") : t("{to} takes over {player}"),
        { to: user(e.to), player: player(e.player), user: user(e.from || "") });
    } else {
      const text = { join: t("{player} joined ({game})"), part: t("{player} left"), goal: t("{player} reached their goal"),
        release: t("{player} released the items still in their world"), collect: t("{player} collected their items from the other worlds") }[e.kind];
      body = withNodes(text || "{player}", { player: player(e.player), game: e.game || "" });
    }
    return h("div", { class: `msg ${e.kind === "goal" ? "goal" : ""}` }, h("span", { class: "time" }, clock(e.at)), body);
  }

  function draw(jump = false) {
    clear(tabs).append(filters.map(([key, label]) => h("button", { class: "tab", type: "button", role: "tab",
      "aria-selected": String(key === filter), "aria-current": key === filter ? "page" : undefined,
      onclick: () => { filter = key; draw(true); } }, label())));
    const test = filters.find(([key]) => key === filter)[2];
    const shown = entries.filter((e) => test(e) && (!onlyMine?.input.checked || concerns(e)));
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    clear(list).append(shown.length ? shown.map(line)
      : h("div", { class: "note" }, !loaded ? t("Loading…") : spectator ? t("Nothing yet. Items, hints and goals show up here as they happen.")
        : t("Nothing yet. Chat, items, hints and goals show up here as they happen.")));
    if (jump || atBottom) list.scrollTop = list.scrollHeight;
  }

  async function load() {
    try {
      const fresh = await get(`${source}?after=${last}`);
      if (!isCurrent()) return;
      if (fresh.last < last) {  // the feed started over (the game was set up again)
        entries = [];
        last = 0;
        return load();
      }
      const first = !loaded;
      loaded = true;
      if (fresh.entries.length || first) {
        entries = [...entries, ...fresh.entries].slice(-KEEP);
        last = fresh.last;
        draw(first);
      }
      const count = !first && document.hidden && onUnread ? fresh.entries.filter(news).length : 0;
      if (count) onUnread((unread += count));
    } catch { /* the next look tries again */ }
  }
  draw();
  load();
  // in the background only to count the news, and less often
  let ticks = 0;
  timer = setInterval(() => { if (!document.hidden || (onUnread && ++ticks % 4 === 0)) load(); }, 5000);
  cleanup(() => clearInterval(timer));
  if (onUnread) {
    const seen = () => { if (!document.hidden && unread) onUnread((unread = 0)); };
    document.addEventListener("visibilitychange", seen);
    cleanup(() => document.removeEventListener("visibilitychange", seen));
  }

  return h("div", { class: "section" }, h("div", { class: "card stack" },
    h("div", { class: "card-head" }, h("h2", {}, t("Activity")), onlyMine),
    h("p", { class: "muted small" }, spectator ? t("What happens in the multiworld: items sent and found, hints, and who joins and finishes.")
      : t("What happens in the multiworld: chat, items sent and found, hints, and who joins and finishes.")),
    tabs, list));
}
