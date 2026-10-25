// Personal API tokens (routes/tokens.py): for scripts and bots that use this server's API in the user's name.
import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo, fmtDate } from "../util.js";
import { get, post, del, isAdmin } from "../api.js";
import { pageHeader, badge, field, select, copyField, empty } from "../components.js";
import { t } from "../i18n.js";

const EXPIRY = [["90", t("90 days")], ["30", t("30 days")], ["365", t("1 year")], ["", t("Never")]];

export default async function render({ root, isCurrent }) {
  const admin = isAdmin();
  const mineHost = h("div");
  const allHost = h("div");

  const name = h("input", { class: "input", maxLength: 60, placeholder: t("e.g. Discord bot, status page") });
  const access = select([["read", t("Read only")], ["write", t("Read and change")]], "read");
  const expires = select(EXPIRY, "90");
  const create = h("button", { class: "btn primary", type: "button" }, icon("plus", 14), t("Create token"));
  create.onclick = action(async () => {
    if (!name.value.trim()) throw new Error(t("Give the token a name, such as what uses it"));
    const made = await post("/api/tokens", { name: name.value, access: access.value,
      expires_days: expires.value ? Number(expires.value) : null });
    name.value = "";
    showToken(made);
    await load();
  });

  function showToken(made) {
    const modal = openModal({
      title: t("Your new token"),
      body: h("div", { class: "stack" },
        h("p", {}, t("Copy it now: it is not shown again. Whoever has it can use this server as you ({access}).",
          { access: made.access === "write" ? t("read and change") : t("read only") })),
        copyField(made.token),
        h("pre", { class: "console", style: { margin: 0, whiteSpace: "pre-wrap", height: "auto", minHeight: 0 } },
          `curl -H "Authorization: Bearer ${made.token}" ${location.origin}/api/games`)),
      actions: [h("button", { class: "btn primary", type: "button", onclick: () => modal.close() }, t("Done"))],
    });
  }

  function table(tokens, withOwner) {
    if (!tokens.length) return empty(t("No tokens yet"), t("Create one above for a script or a bot."));
    return h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [withOwner ? t("User") : null, t("Name"), t("Access"), t("Created"), t("Last used"), t("Expires"), ""]
        .filter((x) => x !== null).map((text) => h("th", {}, text)))),
      h("tbody", {}, tokens.map((tok) => h("tr", { style: tok.expired ? { opacity: 0.6 } : undefined },
        withOwner ? h("td", {}, tok.username) : null,
        h("td", {}, h("span", { class: "slot-name" }, tok.name), h("div", { class: "muted small mono" }, `apm_…${tok.hint}`)),
        h("td", {}, tok.access === "write" ? badge(t("read and change"), "warn") : badge(t("read only"), "muted")),
        h("td", { class: "muted" }, fmtDate(tok.created_at)),
        h("td", { class: "muted" }, tok.last_used_at ? [fmtAgo(tok.last_used_at), tok.last_used_ip ? h("div", { class: "small" }, tok.last_used_ip) : null] : t("never")),
        h("td", {}, tok.expired ? badge(t("expired"), "bad") : tok.expires_at ? h("span", { class: "muted" }, fmtDate(tok.expires_at)) : h("span", { class: "muted" }, t("never"))),
        h("td", { class: "actions" }, h("button", { class: "btn sm", type: "button", onclick: action(async () => {
          if (!(await confirmDialog(t("Whatever uses “{name}” can no longer reach this server.", { name: tok.name }),
            { title: t("Revoke this token?"), danger: true, confirmText: t("Revoke") }))) return;
          await del(`/api/tokens/${tok.id}`);
          toast(t("Token revoked"), "success");
          await load();
        }) }, icon("trash", 14), t("Revoke"))))))));
  }

  async function load() {
    const [mine, all] = await Promise.all([get("/api/tokens"), admin ? get("/api/tokens?all=true") : null]);
    if (!isCurrent()) return;
    clear(mineHost).append(table(mine, false));
    if (admin) {
      clear(allHost).append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Everybody's tokens"))),
        h("p", { class: "muted small" }, t("As an administrator you can revoke any user's token, for example one that leaked.")),
        table(all, true)));
    }
  }

  root.append(
    pageHeader(t("API tokens"), t("For scripts and bots that use this server in your name.")),
    h("div", { class: "card stack" },
      h("h2", {}, t("New token")),
      h("div", { class: "form-grid" }, field(t("Name"), name),
        field(t("Access"), access, t("Read only: look things up. Read and change: everything you can do here, such as starting games.")),
        field(t("Expires after"), expires)),
      h("div", { class: "row" }, create)),
    h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Your tokens"))), mineHost),
    allHost,
    h("div", { class: "card stack section" },
      h("h2", {}, t("How to use a token")),
      h("p", { class: "muted small" }, t("Send it in the Authorization header of each request to /api/…. The answers are JSON, the same the web UI gets. Tokens cannot make tokens, change passwords or sign in and out. A token stops working when it is revoked, expires, or its user is disabled.")),
      h("pre", { class: "console", style: { margin: 0, whiteSpace: "pre-wrap", height: "auto", minHeight: 0 } },
        `curl -H "Authorization: Bearer apm_…" ${location.origin}/api/games\n`
        + `curl -X POST -H "Authorization: Bearer apm_…" ${location.origin}/api/games/1/start`)));
  await load();
}
