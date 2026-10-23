// What happened while the player was away (visits.py on the server): shown at the top of the game page when they come
// back, until they put it away. The page reports a visit when it opens and every minute while it is in view.
import { h, clear, icon, fmtAgo, fmtDate, withNodes } from "./util.js";
import { post } from "./api.js";
import { t, plural } from "./i18n.js";

const HEARTBEAT = 60000;

export function awaySummary({ gameId, cleanup, isCurrent }) {
  const host = h("div");
  const player = (name) => h("span", { class: "slot-name" }, name);
  const item = (name) => h("strong", {}, name);

  function draw(summary) {
    clear(host);
    if (!summary) return;
    const lines = [];
    const { received } = summary;
    if (received.count) {
      lines.push(h("li", {}, plural(received.count, "You received {n} item:", "You received {n} items:"),
        h("ul", { class: "away-items" }, received.items.map((e) => h("li", {},
          withNodes(t("{item} from {finder}"), { item: item(e.item), finder: player(e.finder) }),
          h("span", { class: "muted small" }, ` · ${e.location}`)))),
        received.count > received.items.length
          ? h("span", { class: "muted small" }, t("…and {n} more", { n: received.count - received.items.length })) : null));
    }
    if (summary.hint_count) {
      lines.push(h("li", {}, plural(summary.hint_count, "{n} open hint concerns you:", "{n} open hints concern you:"),
        h("ul", { class: "away-items" }, summary.hints.map((e) => h("li", {}, e.in_my_world
          ? withNodes(t("{receiver} waits for their {item} at {location} in your world"),
            { receiver: player(e.receiver), item: item(e.item), location: e.location })
          : withNodes(t("Your {item} is at {location} in {finder}'s world"),
            { item: item(e.item), location: e.location, finder: player(e.finder) }))))));
    }
    for (const s of summary.stuck) {
      lines.push(h("li", {}, withNodes(s.note ? t("{player} is stuck: {note}") : t("{player} is stuck"),
        { player: player(s.player), note: s.note || "" }), h("span", { class: "muted small" }, ` · ${fmtAgo(s.at)}`)));
    }
    if (summary.goals.length) {
      lines.push(h("li", {}, withNodes(plural(summary.goals.length, "{players} reached their goal", "{players} reached their goals"),
        { players: summary.goals.map(player).flatMap((node, i) => (i ? [", ", node] : [node])) })));
    }
    for (const r of summary.released) {
      lines.push(h("li", {}, withNodes(r.kind === "release" ? t("{player} released the items still in their world")
        : t("{player} collected their items from the other worlds"), { player: player(r.player) })));
    }
    if (summary.chat) lines.push(h("li", {}, plural(summary.chat, "{n} new message in the chat", "{n} new messages in the chat")));

    const dismiss = h("button", { class: "btn sm", type: "button" }, icon("check", 14), t("Got it"));
    dismiss.onclick = async () => {
      clear(host);
      await post(`/api/games/${gameId}/visit`, { dismiss: true }).catch(() => {});
    };
    host.append(h("div", { style: { marginBottom: "28px" } }, h("div", { class: "card stack away" },
      h("div", { class: "card-head" },
        h("div", {}, h("h2", {}, t("While you were away")),
          h("div", { class: "muted small", title: fmtDate(summary.since) }, t("Your last visit: {ago}", { ago: fmtAgo(summary.since) }))),
        dismiss),
      h("ul", { class: "away-list" }, lines),
      summary.partial ? h("p", { class: "muted small" }, t("Only the latest activity is kept, so this may not be everything.")) : null)));
  }

  const visit = () => post(`/api/games/${gameId}/visit`, {});
  visit().then((r) => { if (isCurrent()) draw(r.summary); }).catch(() => {});
  // while the page is in view the player is here; a hidden tab counts as away
  const timer = setInterval(() => { if (!document.hidden) visit().catch(() => {}); }, HEARTBEAT);
  cleanup(() => clearInterval(timer));
  return host;
}
