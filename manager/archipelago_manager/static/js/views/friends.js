// Friends (routes/friends.py): ask someone by their user name, answer requests, and see who you are friends with.
// Friends can share configs with each other alone, and generate games with each other in (views/generate.js).
import { h, clear, icon, toast, action, confirmDialog, fmtAgo, fmtDate } from "../util.js";
import { get, post, del } from "../api.js";
import { pageHeader, empty } from "../components.js";
import { t, plural } from "../i18n.js";
import { every } from "../live.js";

export default async function render({ root, cleanup, isCurrent }) {
  const requestsHost = h("div");
  const friendsHost = h("div");
  const waitingHost = h("div");
  const suggestionsHost = h("div");
  let signature = "";

  async function load(force = false) {
    const data = await get("/api/friends");
    if (!isCurrent()) return;
    document.dispatchEvent(new CustomEvent("apm-friend-requests", { detail: data.incoming.length }));  // the user button (app.js)
    const sig = JSON.stringify(data);
    if (!force && sig === signature) return;
    signature = sig;
    draw(data);
  }

  const ask = async (username) => {
    const result = await post("/api/friends", { username });
    toast(result.status === "accepted" ? t("You and {name} are friends now", { name: result.user.username })
      : t("Friend request sent to {name}", { name: result.user.username }), "success");
    await load(true);
  };

  // asking by name ------------------------------------------------------------------------------------------------------
  const nameInput = h("input", { class: "input", id: "friend-name", maxLength: 64, autocomplete: "off", placeholder: t("Their user name") });
  const send = h("button", { class: "btn primary", type: "submit" }, icon("plus", 14), t("Send a friend request"));
  // the button beside the name (below it on a phone), the hint under both
  const form = h("form", { class: "field" }, h("label", { class: "field-label", for: "friend-name" }, t("User name")),
    h("div", { class: "row" }, h("div", { class: "grow", style: { minWidth: "180px" } }, nameInput), send),
    h("span", { class: "hint" }, t("As it shows next to their slots in a game.")));
  const sendRequest = action(async () => {
    const username = nameInput.value.trim();
    if (!username) throw new Error(t("Enter the user name of the one you want to add"));
    await ask(username);
    nameInput.value = "";
  });
  form.onsubmit = (e) => { e.preventDefault(); sendRequest({ currentTarget: send }); };

  const person = (p, ...details) => {
    const parts = details.filter(Boolean);
    return h("div", {}, h("span", { class: "slot-name" }, p.username),
      parts.length ? h("div", { class: "muted small" }, parts.flatMap((part, i) => (i ? [" · ", part] : [part]))) : null);
  };
  const list = (rows) => h("div", { class: "card stack tight" }, rows);

  function draw(data) {
    clear(requestsHost);
    if (data.incoming.length) {
      requestsHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Friend requests"))),
        list(data.incoming.map((p) => h("div", { class: "row spread" },
          person(p, t("asked {ago}", { ago: fmtAgo(p.asked_at) })),
          h("div", { class: "row" },
            h("button", { class: "btn sm primary", type: "button", onclick: action(async () => {
              await post(`/api/friends/${p.id}/accept`);
              toast(t("You and {name} are friends now", { name: p.username }), "success");
              await load(true);
            }) }, icon("check", 14), t("Accept")),
            h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              await del(`/api/friends/${p.id}`);
              toast(t("Request from {name} declined", { name: p.username }), "success");
              await load(true);
            }) }, t("Decline"))))))));
    }

    clear(friendsHost).append(h("div", { class: "section-head" }, h("h2", {}, t("Your friends"))));
    if (!data.friends.length) {
      friendsHost.append(empty(t("No friends yet"),
        t("Add the people you play with. Once they accept, you can share configs with them alone and generate games with them in.")));
    } else {
      friendsHost.append(list(data.friends.map((f) => h("div", { class: "row spread" },
        person(f, t("friends since {date}", { date: fmtDate(f.since) }),
          f.shared_configs ? h("a", { href: "#/configs?shared=1" }, plural(f.shared_configs, "shares {n} config with you", "shares {n} configs with you")) : null,
          f.games_together ? plural(f.games_together, "{n} game together", "{n} games together") : null),
        h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => {
          if (!(await confirmDialog(t("Configs you share with {name} by name are no longer shared with them, and theirs with you.", { name: f.username }),
            { title: t("Remove {name} from your friends?", { name: f.username }), danger: true, confirmText: t("Remove") }))) return;
          await del(`/api/friends/${f.id}`);
          toast(t("{name} is no longer your friend", { name: f.username }), "success");
          await load(true);
        }) }, icon("trash", 14), t("Remove"))))));
    }

    clear(waitingHost);
    if (data.outgoing.length) {
      waitingHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Waiting for an answer"))),
        list(data.outgoing.map((p) => h("div", { class: "row spread" },
          person(p, t("asked {ago}", { ago: fmtAgo(p.asked_at) })),
          h("button", { class: "btn sm", type: "button", onclick: action(async () => {
            await del(`/api/friends/${p.id}`);
            toast(t("Request to {name} taken back", { name: p.username }), "success");
            await load(true);
          }) }, t("Take back")))))));
    }

    clear(suggestionsHost);
    if (data.suggestions.length) {
      suggestionsHost.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("People you played with"))),
        list(data.suggestions.map((p) => h("div", { class: "row spread" },
          person(p, plural(p.games_together, "{n} game together", "{n} games together")),
          h("button", { class: "btn sm", type: "button", onclick: action(() => ask(p.username)) }, icon("plus", 14), t("Add")))))));
    }
  }

  root.append(
    pageHeader(t("Friends"), t("The people you play with: share configs with them alone, and generate games with them in.")),
    h("div", { class: "card stack" }, h("h2", {}, t("Add a friend")), form),
    requestsHost,
    h("div", { class: "section" }, friendsHost),
    waitingHost,
    suggestionsHost);
  await load(true);
  every(10000, () => load(), cleanup);
}

