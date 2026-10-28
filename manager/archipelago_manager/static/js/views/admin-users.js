import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo, fmtDate } from "../util.js";
import { get, post, patch, del, state } from "../api.js";
import { pageHeader, badge, field, select, adminTabs, copyField } from "../components.js";
import { t, plural } from "../i18n.js";

// Organizers run sign-up rounds and the games they add or generate, without administering the server
const ROLES = [["user", t("User")], ["organizer", t("Organizer")], ["admin", t("Administrator")]];

// What an invite link makes: never an administrator (routes/auth.py, invites.py)
const INVITE_ROLES = [["user", t("User")], ["organizer", t("Organizer")]];

// A link to this server's web UI with a token in its fragment, which browsers never send to a server
const linkTo = (kind, token) => `${state.status.site_url || location.origin}/#/${kind}/${token}`;

export default async function render({ root, isCurrent }) {
  const host = h("div");
  const invitesHost = h("div", { class: "section" });

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

  // A reset link, with which they set a password themselves, or a password set right here
  function resetDialog(user) {
    const linkHost = h("div", { class: "stack tight" });
    const makeLink = h("button", { class: "btn", type: "button", disabled: user.disabled,
      title: user.disabled ? t("Enable the account first") : undefined }, icon("link", 14), t("Make a reset link"));
    makeLink.onclick = action(async () => {
      const result = await post(`/api/users/${user.id}/reset-link`);
      clear(linkHost).append(copyField(linkTo("reset", result.token)),
        h("span", { class: "muted small" }, t("Shown only now: copy it before you close this. It works until {date}.", { date: fmtDate(result.expires_at) })));
      makeLink.textContent = t("Make a new reset link");
    });
    const pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const save = h("button", { class: "btn", type: "button" }, t("Set password"));
    save.onclick = action(async () => {
      await patch(`/api/users/${user.id}`, { password: pw.value });
      modal.close();
      toast(t("Password for {user} changed. They have been signed out, and their API tokens were revoked.", { user: user.username }), "success");
    });
    const modal = openModal({ title: t("New password for {user}", { user: user.username }),
      body: h("div", { class: "stack" },
        h("div", { class: "stack tight" }, h("strong", {}, t("Let them choose it")),
          h("p", { class: "muted small" }, t("A link with which {user} sets a new password, once, within a day. Send it to them, on Discord say. Setting the password signs them out everywhere and revokes their API tokens. A new link ends the one before.", { user: user.username })),
          h("div", { class: "row" }, makeLink), linkHost),
        h("hr"),
        h("div", { class: "stack tight" }, h("strong", {}, t("Or set it yourself")),
          field(t("New password"), pw, t("At least 8 characters")), h("div", { class: "row" }, save))),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close"))] });
  }

  // Invite links: whoever has one makes their own account, also while registration is closed
  async function loadInvites() {
    const list = await get("/api/users/invites");
    if (!isCurrent()) return;
    const stateOf = (i) => (i.state === "active" ? h("span", { class: "small" }, t("until {date}", { date: fmtDate(i.expires_at) }))
      : badge(i.state === "used_up" ? t("used up") : t("expired"), "muted"));
    clear(invitesHost).append(h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, t("Invite links")),
        h("button", { class: "btn sm", type: "button", onclick: inviteDialog }, icon("plus", 14), t("Invite"))),
      h("p", { class: "muted small" }, t("Whoever has an invite link makes their own account with it, also while registration is closed, with Discord too if signing in with Discord is set up. A link can be used as often as you allow, until it expires.")),
      list.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Note"), t("Role"), t("Used"), t("Valid"), t("Joined"), ""].map((text) => h("th", {}, text)))),
        h("tbody", {}, list.map((i) => h("tr", { style: i.state === "active" ? undefined : { opacity: 0.65 } },
          h("td", {}, i.note || h("span", { class: "muted" }, "—"), h("div", { class: "muted small" },
            i.created_by ? t("by {name}, {ago}", { name: i.created_by, ago: fmtAgo(i.created_at) }) : fmtAgo(i.created_at))),
          h("td", {}, (INVITE_ROLES.find(([key]) => key === i.role) || [null, i.role])[1]),
          h("td", {}, `${i.uses} / ${i.max_uses}`),
          h("td", {}, stateOf(i)),
          h("td", {}, i.joined.length ? i.joined.join(", ") : h("span", { class: "muted" }, "—")),
          h("td", { class: "actions" }, h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => {
            if (i.state === "active" && !(await confirmDialog(t("The link stops working. Accounts made with it stay."),
              { title: t("Revoke this invite link?"), confirmText: t("Revoke") }))) return;
            await del(`/api/users/invites/${i.id}`);
            await loadInvites();
          }) }, i.state === "active" ? t("Revoke") : t("Remove")))))))) : h("p", { class: "muted" }, t("No invite links yet."))));
  }

  function inviteDialog() {
    const role = select(INVITE_ROLES, "user");
    const uses = h("input", { class: "input", type: "number", min: 1, max: 100, value: 1 });
    const days = select([1, 7, 30].map((n) => [n, plural(n, "{n} day", "{n} days")]), 7);
    const note = h("input", { class: "input", maxLength: 100, autocomplete: "off", placeholder: t("For example: the Friday group") });
    const make = h("button", { class: "btn primary", type: "button" }, icon("link", 14), t("Make the link"));
    const body = h("div", { class: "stack" },
      h("div", { class: "form-grid" }, field(t("Role"), role, t("Administrators are made under Users, by hand.")),
        field(t("How many accounts"), uses, t("1 for one person, more for a group")), field(t("Valid for"), days), field(t("Note"), note, t("For you, to tell your links apart; shown on the invitation too."))));
    make.onclick = action(async () => {
      const result = await post("/api/users/invites", { role: role.value, max_uses: Number(uses.value), days: Number(days.value), note: note.value });
      clear(body).append(h("p", {}, t("Send this link to whoever should join.")), copyField(linkTo("invite", result.token)),
        h("p", { class: "muted small" }, t("Shown only now: copy it before you close this. Only a fingerprint of it is kept, so nobody can read it from the server later.")));
      make.remove();
      await loadInvites();
    });
    const modal = openModal({ title: t("Invite someone"), body,
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close")), make] });
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
    h("button", { class: "btn", type: "button", onclick: inviteDialog }, icon("link", 14), t("Invite")),
    h("button", { class: "btn primary", type: "button", onclick: addDialog }, icon("plus", 14), t("Add user"))),
    adminTabs("users"), host, invitesHost);
  await Promise.all([load(), loadInvites()]);
}
