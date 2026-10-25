import { h, clear, icon, fmtAgo } from "../util.js";
import { get, isOrganizer } from "../api.js";
import { pageHeader, empty, chips, badge } from "../components.js";
import { openSignupForm, signupBadge, deadlineText } from "./signup.js";
import { t, plural } from "../i18n.js";

export function signupCard(s) {
  return h("div", { class: "card game-card" },
    h("div", { class: "card-head" },
      h("div", { class: "stack tight" }, h("h3", {}, h("a", { href: `#/signups/${s.id}` }, s.name)),
        s.mystery ? h("span", {}, badge(t("Mystery"), "accent")) : s.allowed_games.length ? chips(s.allowed_games) : h("span", { class: "muted small" }, t("Any game"))),
      signupBadge(s)),
    h("div", { class: "muted small" }, deadlineText(s) || t("Created {ago}", { ago: fmtAgo(s.created_at) })),
    h("div", {}, plural(s.entry_count, "{n} entry", "{n} entries"),
      s.my_entry_count ? h("span", { class: "muted" }, " · ", s.my_entry_count > 1 ? t("you are in with {n}", { n: s.my_entry_count }) : t("you are in")) : null),
    h("div", { class: "row" },
      h("a", { class: `btn sm ${s.open && !s.my_entry_count ? "primary" : ""}`, href: `#/signups/${s.id}` },
        s.open && !s.my_entry_count ? t("Join") : t("Open")),
      s.game_id ? h("a", { class: "btn sm", href: `#/games/${s.game_id}` }, t("Game")) : null));
}

export default async function render({ root, cleanup, isCurrent }) {
  const organizer = isOrganizer();  // opens rounds
  const host = h("div");
  let signature = "";

  async function load(force = false) {
    const list = await get("/api/signups");
    if (!isCurrent()) return;
    const sig = JSON.stringify(list);
    if (!force && sig === signature) return;
    signature = sig;
    clear(host);
    if (!list.length) {
      host.append(empty(t("No sign-up rounds yet"),
        organizer ? t("Open a round: players enter their YAML until the deadline, then the game is generated from all entries.")
              : t("When an administrator opens a round, you can enter your config here."),
        organizer ? h("button", { class: "btn primary", type: "button", onclick: create }, icon("plus", 14), t("New round")) : null));
      return;
    }
    const active = list.filter((s) => s.status !== "done");
    const done = list.filter((s) => s.status === "done");
    if (active.length) host.append(h("div", { class: "grid" }, active.map(signupCard)));
    if (done.length) {
      host.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Finished rounds"))),
        h("div", { class: "grid" }, done.map(signupCard))));
    }
  }

  function create() {
    openSignupForm({ onSaved: (s) => { location.hash = `#/signups/${s.id}`; } }).catch(() => {});
  }

  root.append(
    pageHeader(t("Sign-ups"), t("Rounds that collect everyone's YAML until a deadline, then generate one game from them."),
      organizer ? h("button", { class: "btn primary", type: "button", onclick: create }, icon("plus", 14), t("New round")) : null),
    host);
  await load(true);
  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 8000);
  cleanup(() => clearInterval(timer));
}
