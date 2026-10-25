import { h, clear, icon, fmtBytes } from "../util.js";
import { get, state, isAdmin } from "../api.js";
import { pageHeader, missingGamesPanel, gameCard, empty, updateBanner } from "../components.js";
import { signupCard } from "./signups.js";
import { t, plural } from "../i18n.js";

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
  const [games, missing, configs, system, rounds] = await Promise.all([
    get("/api/games"), get("/api/missing-games"), get("/api/configs"), admin ? get("/api/system") : null, get("/api/signups"),
  ]);

  const gridHost = h("div");
  const signupsHost = h("div");
  const drawSignups = (list) => {
    const open = list.filter((s) => s.status === "open");
    clear(signupsHost);
    if (open.length) {
      signupsHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Open sign-up rounds")),
        h("a", { href: "#/signups" }, t("See all"))), h("div", { class: "grid" }, open.map(signupCard))));
    }
  };
  const missingHost = h("div");
  const drawMissing = (items) => clear(missingHost).append(missingGamesPanel(items, load) || "");
  const drawGames = (all) => {
    const list = all.filter((g) => !g.finished_at);  // finished ones are in the archive under Games
    const archived = all.length - list.length;
    const mine = list.filter((g) => g.my_slots.length);
    const others = list.filter((g) => !g.my_slots.length);
    const onUpdate = () => load();
    clear(gridHost);
    const offered = list.flatMap((g) => g.offered_slots.map((o) => ({ ...o, gameId: g.id, gameName: g.name })));
    if (offered.length) {
      gridHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Slots looking for a player"))),
        h("div", { class: "card stack tight" }, offered.map((o) => h("div", { class: "row spread" },
          h("div", {}, h("span", { class: "slot-name" }, o.name), h("span", { class: "muted" }, ` · ${o.game} · ${o.gameName}`),
            h("div", { class: "muted small" }, o.to_me ? t("{owner} wants to hand it over to you", { owner: o.from })
              : t("{owner} hands it over to whoever takes it", { owner: o.from }), o.note ? ` · “${o.note}”` : "")),
          h("a", { class: "btn sm primary", href: `#/games/${o.gameId}` }, t("Have a look")))))));
    }
    if (!all.length) {
      gridHost.append(empty(t("No games yet"),
        admin ? t("Upload a pregenerated multiworld or generate a new one.") : t("When an administrator adds you to a game, it shows up here."),
        admin ? h("a", { class: "btn primary", href: "#/games" }, icon("upload", 14), t("Add a game")) : null,
        admin ? h("a", { class: "btn", href: "#/generate" }, icon("zap", 14), t("Generate")) : null));
      return;
    }
    if (mine.length) {
      gridHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Your games"))),
        h("div", { class: "grid" }, mine.map((g) => gameCard(g, onUpdate)))));
    }
    if (others.length) {
      gridHost.append(h("div", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, mine.length ? t("Other games") : t("Games")),
          h("a", { href: "#/games" }, t("See all"))),
        h("div", { class: "grid" }, others.slice(0, 6).map((g) => gameCard(g, onUpdate)))));
    }
    if (archived) {
      gridHost.append(h("p", { class: "muted small" }, h("a", { href: "#/games" },
        plural(archived, "{n} finished game is in the archive", "{n} finished games are in the archive"))));
    }
  };
  async function load() {
    const [g, m, s] = await Promise.all([get("/api/games"), get("/api/missing-games"), get("/api/signups")]);
    if (!isCurrent()) return;
    drawGames(g);
    drawMissing(m);
    drawSignups(s);
  }

  root.append(
    pageHeader(t("Welcome, {name}", { name: state.user.username }), admin ? t("Overview of this server.") : t("Your games and player configs.")),
    system?.update?.update_available ? h("div", { class: "page-banner" }, updateBanner(system.update)) : null,
    missingHost,
    system ? h("div", { class: "grid tiles" },
      tile(`${system.running_games} / ${system.games}`, t("Games running")),
      tile(system.users, t("Users")),
      tile(system.ap_version ? `${system.ap_version}` : t("unavailable"), t("Archipelago · {n} games", { n: system.world_count })),
      tile(fmtBytes(system.disk.free), t("Disk free"))) : null,
    signupsHost,
    gridHost);
  drawMissing(missing);
  drawSignups(rounds);
  drawGames(games);

  if (!configs.length) {
    root.append(h("div", { class: "section" }, h("div", { class: "card stack" },
      h("h2", {}, t("Get started")),
      h("p", {}, t("Players need a YAML file that describes how their game should be randomized. Upload yours or create one here.")),
      h("div", { class: "row" },
        h("a", { class: "btn primary", href: "#/configs" }, icon("upload", 14), t("Upload a YAML")),
        h("a", { class: "btn", href: "#/configs/new" }, icon("file", 14), t("Create a config"))))));
  }

  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 6000);
  cleanup(() => clearInterval(timer));
}

function tile(value, label) {
  return h("div", { class: "tile" }, h("div", { class: "value" }, String(value)), h("div", { class: "label" }, label));
}
