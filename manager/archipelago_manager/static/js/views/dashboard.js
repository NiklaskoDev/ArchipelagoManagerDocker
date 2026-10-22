import { h, clear, icon, fmtBytes } from "../util.js";
import { get, state, isAdmin } from "../api.js";
import { pageHeader, missingGamesPanel, gameCard, empty, updateBanner } from "../components.js";
import { signupCard } from "./signups.js";

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
      signupsHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Open sign-ups"),
        h("a", { href: "#/signups" }, "See all")), h("div", { class: "grid" }, open.map(signupCard))));
    }
  };
  const missingHost = h("div");
  const drawMissing = (items) => clear(missingHost).append(missingGamesPanel(items, load) || "");
  const drawGames = (list) => {
    const mine = list.filter((g) => g.my_slots.length);
    const others = list.filter((g) => !g.my_slots.length);
    const onUpdate = () => load();
    clear(gridHost);
    if (!list.length) {
      gridHost.append(empty("No games yet",
        admin ? "Upload a pregenerated multiworld or generate a new one." : "When an administrator adds you to a game, it shows up here.",
        admin ? h("a", { class: "btn primary", href: "#/games" }, icon("upload", 14), "Add a game") : null,
        admin ? h("a", { class: "btn", href: "#/generate" }, icon("zap", 14), "Generate") : null));
      return;
    }
    if (mine.length) {
      gridHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Your games")),
        h("div", { class: "grid" }, mine.map((g) => gameCard(g, onUpdate)))));
    }
    if (others.length) {
      gridHost.append(h("div", { class: "section" },
        h("div", { class: "section-head" }, h("h2", {}, mine.length ? "Other games" : "Games"),
          h("a", { href: "#/games" }, "See all")),
        h("div", { class: "grid" }, others.slice(0, 6).map((g) => gameCard(g, onUpdate)))));
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
    pageHeader(`Welcome, ${state.user.username}`, admin ? "Overview of this server." : "Your games and player configs."),
    system?.update?.update_available ? h("div", { class: "page-banner" }, updateBanner(system.update)) : null,
    missingHost,
    system ? h("div", { class: "grid tiles" },
      tile(`${system.running_games} / ${system.games}`, "Games running"),
      tile(system.users, "Users"),
      tile(system.ap_version ? `${system.ap_version}` : "unavailable", `Archipelago · ${system.world_count} games`),
      tile(fmtBytes(system.disk.free), "Disk free")) : null,
    signupsHost,
    gridHost);
  drawMissing(missing);
  drawSignups(rounds);
  drawGames(games);

  if (!configs.length) {
    root.append(h("div", { class: "section" }, h("div", { class: "card stack" },
      h("h2", {}, "Get started"),
      h("p", {}, "Players need a YAML file that describes how their game should be randomized. Upload yours or start from a template."),
      h("div", { class: "row" },
        h("a", { class: "btn primary", href: "#/configs" }, icon("upload", 14), "Upload a YAML"),
        h("a", { class: "btn", href: "#/configs/new" }, icon("file", 14), "Create from template")))));
  }

  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 6000);
  cleanup(() => clearInterval(timer));
}

function tile(value, label) {
  return h("div", { class: "tile" }, h("div", { class: "value" }, String(value)), h("div", { class: "label" }, label));
}
