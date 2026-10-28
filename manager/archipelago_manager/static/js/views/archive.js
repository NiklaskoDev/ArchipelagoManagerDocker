import { h, clear, fmtDate, fmtSpan } from "../util.js";
import { get } from "../api.js";
import { pageHeader, empty, chips, badge, gamesTabs } from "../components.js";
import { t } from "../i18n.js";

// Games that are over: every player reached their goal ("finished"), or they were moved here before that ("aborted")
const OUTCOMES = [["all", () => t("All")], ["finished", () => t("Finished")], ["aborted", () => t("Aborted")]];

export default async function render({ root, cleanup, isCurrent }) {
  let games = [], shown = "all", signature = "";
  const host = h("div");
  const tabsHost = h("div");
  const search = h("input", { class: "input", type: "search", placeholder: t("Search by game, world or player…"),
    "aria-label": t("Search the archive") });
  const filters = h("div", { class: "tabs", role: "tablist", style: { marginBottom: 0 } });
  search.oninput = draw;
  // made once, so the search keeps the focus while the list below it is drawn anew
  const listHost = h("div");
  const card = h("div", { class: "card flush" },
    h("div", { class: "row spread list-tools" }, filters, h("div", { class: "grow list-search" }, search)), listHost);

  async function load(force = false) {
    const all = await get("/api/games");
    if (!isCurrent()) return;
    const sig = JSON.stringify(all);
    if (!force && sig === signature) return;
    signature = sig;
    games = all.filter((g) => g.finished_at).sort((a, b) => b.finished_at - a.finished_at);
    clear(tabsHost).append(gamesTabs("archive", games.length));
    draw();
  }

  function draw() {
    const counts = { all: games.length, finished: 0, aborted: 0 };
    for (const g of games) counts[g.outcome] += 1;
    clear(filters).append(OUTCOMES.map(([key, label]) => h("button", { class: "tab", type: "button", role: "tab",
      "aria-selected": String(key === shown), "aria-current": key === shown ? "page" : undefined,
      onclick: () => { shown = key; draw(); } }, `${label()} (${counts[key]})`)));
    if (!games.length) {
      clear(host).append(empty(t("Nothing in the archive yet"),
        t("A game moves here by itself once every player has reached their goal. One that will not be played to the end can be moved here by hand: it is listed as aborted.")));
      return;
    }
    if (card.parentNode !== host) clear(host).append(card);
    const q = search.value.trim().toLowerCase();
    const list = games.filter((g) => (shown === "all" || g.outcome === shown)
      && (!q || `${g.name} ${g.games.join(" ")} ${g.my_slots.map((s) => s.name).join(" ")}`.toLowerCase().includes(q)));
    clear(listHost).append(
      list.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Game"), t("Outcome"), t("Ended"), t("Time played"), t("Goals reached"), t("Worlds"), ""].map((text) => h("th", {}, text)))),
        h("tbody", {}, list.map((g) => h("tr", {},
          h("td", {}, h("a", { href: `#/games/${g.id}`, class: "slot-name" }, g.name),
            g.my_slots.length ? h("div", { class: "muted small" }, t("You: {names}", { names: g.my_slots.map((s) => s.name).join(", ") })) : null),
          h("td", {}, g.outcome === "finished" ? badge(t("Finished"), "ok") : badge(t("Aborted"), "muted")),
          h("td", {}, fmtDate(g.finished_at)),
          h("td", {}, g.play_started_at ? fmtSpan(g.finished_at - g.play_started_at) : h("span", { class: "muted" }, "—")),
          h("td", {}, `${g.goal_count} / ${g.player_count}`),
          h("td", {}, chips(g.games, g.missing_games)),
          h("td", { class: "actions" }, h("a", { class: "btn sm", href: `#/games/${g.id}` }, t("Results"))))))))
        : h("p", { class: "muted list-none" }, t("Nothing matches.")));
  }

  root.append(pageHeader(t("Archive"), t("Games that are over: finished by every player, or aborted before that.")), tabsHost, host);
  await load(true);
  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 15000);
  cleanup(() => clearInterval(timer));
}
