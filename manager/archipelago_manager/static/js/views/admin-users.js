import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo } from "../util.js";
import { get, post, patch, del, state } from "../api.js";
import { pageHeader, badge, field, select, adminTabs } from "../components.js";

export default async function render({ root, isCurrent }) {
  const host = h("div");

  async function load() {
    const users = await get("/api/users");
    if (!isCurrent()) return;
    clear(host).append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["User", "Role", "Configs", "Slots", "Last sign-in", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, users.map((u) => {
        const self = u.id === state.user.id;
        const role = select([["user", "User"], ["admin", "Administrator"]], u.role);
        role.disabled = self;
        role.onchange = async () => {
          try { await patch(`/api/users/${u.id}`, { role: role.value }); toast("Role updated", "success"); } catch (e) { toast(e.message, "error"); }
          await load();
        };
        return h("tr", {},
          h("td", {}, h("span", { class: "slot-name" }, u.username), self ? [" ", badge("you", "accent")] : null, u.disabled ? [" ", badge("disabled", "bad")] : null),
          h("td", {}, role), h("td", {}, u.config_count), h("td", {}, u.slot_count), h("td", { class: "muted" }, fmtAgo(u.last_login)),
          h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            h("button", { class: "btn sm", type: "button", onclick: () => resetDialog(u) }, icon("key", 14), "Password"),
            !self ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              await patch(`/api/users/${u.id}`, { disabled: !u.disabled });
              await load();
            }) }, u.disabled ? "Enable" : "Disable") : null,
            !self ? h("button", { class: "btn sm ghost icon-only", type: "button", title: "Delete user", "aria-label": "Delete user", onclick: action(async () => {
              if (!(await confirmDialog(`Delete ${u.username}? Their ${u.config_count} config(s) are removed and their slots become unowned.`, { danger: true, confirmText: "Delete user" }))) return;
              await del(`/api/users/${u.id}`);
              toast("User deleted", "success");
              await load();
            }) }, icon("trash", 14)) : null)));
      })))));
  }

  function resetDialog(user) {
    const pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const save = h("button", { class: "btn primary", type: "button" }, "Set password");
    save.onclick = action(async () => {
      await patch(`/api/users/${user.id}`, { password: pw.value });
      modal.close();
      toast(`Password for ${user.username} changed. They have been signed out.`, "success");
    });
    const modal = openModal({ title: `New password for ${user.username}`, body: field("New password", pw, "At least 8 characters"),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), save] });
  }

  function addDialog() {
    const username = h("input", { class: "input", autocomplete: "off" });
    const pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const role = select([["user", "User"], ["admin", "Administrator"]], "user");
    const save = h("button", { class: "btn primary", type: "button" }, "Create user");
    save.onclick = action(async () => {
      await post("/api/users", { username: username.value, password: pw.value, role: role.value });
      modal.close();
      toast("User created", "success");
      await load();
    });
    const modal = openModal({ title: "Add a user",
      body: h("div", { class: "stack" }, field("Username", username), field("Password", pw, "At least 8 characters. Share it with the user; they can change it after signing in."), field("Role", role)),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), save] });
  }

  root.append(pageHeader("Administration", "Manage who can use this server.",
    h("button", { class: "btn primary", type: "button", onclick: addDialog }, icon("plus", 14), "Add user")),
    adminTabs("users"), host);
  await load();
}
