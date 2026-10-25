import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo } from "../util.js";
import { get, post, patch, del, state } from "../api.js";
import { pageHeader, badge, field, select, adminTabs } from "../components.js";
import { t, plural } from "../i18n.js";

// Organizers run sign-up rounds and the games they add or generate, without administering the server
const ROLES = [["user", t("User")], ["organizer", t("Organizer")], ["admin", t("Administrator")]];

export default async function render({ root, isCurrent }) {
  const host = h("div");

  async function load() {
    const users = await get("/api/users");
    if (!isCurrent()) return;
    clear(host).append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("User"), t("Role"), t("Configs"), t("Slots"), t("Last sign-in"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, users.map((u) => {
        const self = u.id === state.user.id;
        const role = select(ROLES, u.role);
        role.disabled = self;
        role.onchange = async () => {
          try { await patch(`/api/users/${u.id}`, { role: role.value }); toast(t("Role updated"), "success"); } catch (e) { toast(e.message, "error"); }
          await load();
        };
        return h("tr", {},
          h("td", {}, h("span", { class: "slot-name" }, u.username), self ? [" ", badge(t("you"), "accent")] : null,
            u.disabled ? [" ", badge(t("disabled"), "bad")] : null,
            !u.has_password ? [" ", h("span", { title: t("Signs in with Discord only") }, badge(t("no password")))] : null,
            u.discord_name ? h("div", { class: "muted small" }, t("Discord: {name}", { name: u.discord_name })) : null),
          h("td", {}, role), h("td", {}, u.config_count), h("td", {}, u.slot_count), h("td", { class: "muted" }, fmtAgo(u.last_login)),
          h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            h("button", { class: "btn sm", type: "button", onclick: () => resetDialog(u) }, icon("key", 14), t("Password")),
            u.discord_name ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              const text = t("{user} can no longer sign in with the Discord account {name}, and is signed out everywhere.", { user: u.username, name: u.discord_name })
                + (u.has_password ? "" : ` ${t("They have no password either: set one, or they cannot sign in at all.")}`);
              if (!(await confirmDialog(text, { title: t("Unlink Discord?"), confirmText: t("Unlink") }))) return;
              await patch(`/api/users/${u.id}`, { unlink_discord: true });
              toast(t("Discord unlinked"), "success");
              await load();
            }) }, icon("link", 14), t("Unlink Discord")) : null,
            !self ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              await patch(`/api/users/${u.id}`, { disabled: !u.disabled });
              await load();
            }) }, u.disabled ? t("Enable") : t("Disable")) : null,
            !self ? h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Delete user"), "aria-label": t("Delete user"), onclick: action(async () => {
              const text = plural(u.config_count, "Delete {user}? Their config is removed and their slots become unowned.",
                "Delete {user}? Their {n} configs are removed and their slots become unowned.", { user: u.username });
              if (!(await confirmDialog(text, { danger: true, confirmText: t("Delete user") }))) return;
              await del(`/api/users/${u.id}`);
              toast(t("User deleted"), "success");
              await load();
            }) }, icon("trash", 14)) : null)));
      })))));
  }

  function resetDialog(user) {
    const pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const save = h("button", { class: "btn primary", type: "button" }, t("Set password"));
    save.onclick = action(async () => {
      await patch(`/api/users/${user.id}`, { password: pw.value });
      modal.close();
      toast(t("Password for {user} changed. They have been signed out.", { user: user.username }), "success");
    });
    const modal = openModal({ title: t("New password for {user}", { user: user.username }), body: field(t("New password"), pw, t("At least 8 characters")),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save] });
  }

  function addDialog() {
    const username = h("input", { class: "input", autocomplete: "off" });
    const pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const role = select(ROLES, "user");
    const save = h("button", { class: "btn primary", type: "button" }, t("Create user"));
    save.onclick = action(async () => {
      await post("/api/users", { username: username.value, password: pw.value, role: role.value });
      modal.close();
      toast(t("User created"), "success");
      await load();
    });
    const modal = openModal({ title: t("Add a user"),
      body: h("div", { class: "stack" }, field(t("Username"), username),
        field(t("Password"), pw, t("At least 8 characters. Share it with the user; they can change it after signing in.")),
        field(t("Role"), role, t("Organizers add games, generate and open sign-up rounds, and run those. Users, settings, custom games and ROMs stay with the administrators."))),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save] });
  }

  root.append(pageHeader(t("Administration"), t("Manage who can use this server."),
    h("button", { class: "btn primary", type: "button", onclick: addDialog }, icon("plus", 14), t("Add user"))),
    adminTabs("users"), host);
  await load();
}
