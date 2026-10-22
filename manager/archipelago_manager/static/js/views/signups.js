import { h, clear, icon, fmtAgo } from "../util.js";
import { get, isAdmin } from "../api.js";
import { pageHeader, empty, chips } from "../components.js";
import { openSignupForm, signupBadge, deadlineText } from "./signup.js";

export function signupCard(s) {
  return h("div", { class: "card game-card" },
    h("div", { class: "card-head" },
      h("div", { class: "stack tight" }, h("h3", {}, h("a", { href: `#/signups/${s.id}` }, s.name)),
        s.allowed_games.length ? chips(s.allowed_games) : h("span", { class: "muted small" }, "Any game")),
      signupBadge(s)),
    h("div", { class: "muted small" }, deadlineText(s) || `Created ${fmtAgo(s.created_at)}`),
    h("div", {}, `${s.entry_count} ${s.entry_count === 1 ? "entry" : "entries"}`,
      s.my_entry_count ? h("span", { class: "muted" }, ` · you are in${s.my_entry_count > 1 ? ` with ${s.my_entry_count}` : ""}`) : null),
    h("div", { class: "row" },
      h("a", { class: `btn sm ${s.open && !s.my_entry_count ? "primary" : ""}`, href: `#/signups/${s.id}` },
        s.open && !s.my_entry_count ? "Join" : "Open"),
      s.game_id ? h("a", { class: "btn sm", href: `#/games/${s.game_id}` }, "Game") : null));
}

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
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
      host.append(empty("No sign-up rounds yet",
        admin ? "Open a round: players enter their YAML until the deadline, then the game is generated from all entries."
              : "When an administrator opens a round, you can enter your config here.",
        admin ? h("button", { class: "btn primary", type: "button", onclick: create }, icon("plus", 14), "New round") : null));
      return;
    }
    const active = list.filter((s) => s.status !== "done");
    const done = list.filter((s) => s.status === "done");
    if (active.length) host.append(h("div", { class: "grid" }, active.map(signupCard)));
    if (done.length) {
      host.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Finished rounds")),
        h("div", { class: "grid" }, done.map(signupCard))));
    }
  }

  function create() {
    openSignupForm({ onSaved: (s) => { location.hash = `#/signups/${s.id}`; } }).catch(() => {});
  }

  root.append(
    pageHeader("Sign-ups", "Rounds that collect everyone's YAML until a deadline, then generate one game from them.",
      admin ? h("button", { class: "btn primary", type: "button", onclick: create }, icon("plus", 14), "New round") : null),
    host);
  await load(true);
  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 8000);
  cleanup(() => clearInterval(timer));
}
