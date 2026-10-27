// Who changed what (audit.py): administrators' changes and every start and stop of a game, newest first.
import { h, clear, fmtDate, fmtAgo, fmtBytes } from "../util.js";
import { get } from "../api.js";
import { pageHeader, adminTabs, select, field, empty } from "../components.js";
import { t } from "../i18n.js";

const ACTIONS = {
  "game.add": () => t("Added a game"), "game.settings": () => t("Changed a game's settings"), "game.delete": () => t("Deleted a game"),
  "game.test_notification": () => t("Sent a Discord test message"), "game.start": () => t("Started the server"),
  "game.stop": () => t("Stopped the server"), "game.wake": () => t("Woke the server up"), "game.restart": () => t("Restarted the server"), "game.archive": () => t("Moved a game to the archive or back"), "game.reset": () => t("Reset the progress"),
  "game.backup": () => t("Backed up the progress"), "game.restore": () => t("Restored a backup"), "game.backup_delete": () => t("Deleted a backup"),
  "game.command": () => t("Sent a console command"), "game.file_add": () => t("Added a file"), "game.file_change": () => t("Changed a file"),
  "game.file_delete": () => t("Deleted a file"), "slot.action": () => t("Released or collected items"), "slot.assign": () => t("Assigned a slot"),
  "slot.claim": () => t("Claimed a slot"), "slot.unclaim": () => t("Gave up a slot"), "slot.handover": () => t("Offered a slot to hand over"),
  "slot.handover_cancel": () => t("Took back a handover"), "slot.handover_accept": () => t("Took over a slot"),
  "settings.change": () => t("Changed the settings"), "settings.test_notification": () => t("Sent a Discord test message"),
  "settings.host_yaml": () => t("Edited host.yaml"), "system.check_updates": () => t("Checked for updates"),
  "system.reload_worlds": () => t("Reloaded the worlds"), "system.backup": () => t("Made a backup of everything"), "system.version_install": () => t("Installed an Archipelago version"),
  "system.version_remove": () => t("Removed an Archipelago version"),
  "token.add": () => t("Created an API token"), "token.delete": () => t("Revoked an API token"),
  "user.add": () => t("Added a user"), "user.change": () => t("Changed a user"), "user.delete": () => t("Deleted a user"),
  "apworld.add": () => t("Added a custom game"), "apworld.approve": () => t("Approved a custom game"), "apworld.delete": () => t("Removed a custom game"),
  "source.add": () => t("Added a GitHub repository"), "source.approve": () => t("Approved a GitHub repository"),
  "source.update": () => t("Installed a new release"), "source.check": () => t("Checked for a new release"), "source.delete": () => t("Removed a GitHub repository"),
  "config.change": () => t("Changed a config"), "config.delete": () => t("Deleted a config"), "config.share": () => t("Shared or stopped sharing a config"),
  "job.start": () => t("Started a generation"), "job.cancel": () => t("Cancelled a generation"), "job.delete": () => t("Deleted a generation"),
  "job.preset_add": () => t("Saved a template of generation options"), "job.preset_change": () => t("Changed a template of generation options"),
  "job.preset_delete": () => t("Deleted a template of generation options"),
  "signup.add": () => t("Opened a sign-up round"), "signup.change": () => t("Changed a sign-up round"), "signup.delete": () => t("Deleted a sign-up round"),
  "signup.close": () => t("Closed a sign-up round"), "signup.reopen": () => t("Reopened a sign-up round"), "signup.generate": () => t("Generated a sign-up round"),
  "signup.test": () => t("Test-generated a sign-up round"), "signup.entry_delete": () => t("Removed an entry"),
  "rom.upload": () => t("Uploaded a base ROM"), "rom.delete": () => t("Removed a base ROM"),
};
const AREAS = [["", () => t("Everything")], ["game", () => t("Games")], ["slot", () => t("Slots")], ["job", () => t("Generation")],
  ["signup", () => t("Sign-up rounds")], ["user", () => t("Users")], ["settings", () => t("Settings")], ["apworld", () => t("Custom games")],
  ["source", () => t("GitHub repositories")], ["config", () => t("Configs")], ["rom", () => t("Base ROMs")], ["system", () => t("System")]];
const DETAIL_KEYS = { changed: () => t("changed"), command: () => t("command"), slot: () => t("slot"), role: () => t("role"),
  disabled: () => t("disabled"), password: () => t("password"), discord: () => t("Discord"), game: () => t("game"),
  configs: () => t("configs"), action: () => t("action"), done: () => t("done"), name: () => t("name"), status: () => t("status"),
  to: () => t("to"), previous: () => t("from"), version: () => t("version"),
  token: () => t("API token"), access: () => t("access"), owner: () => t("owner") };

const LINKS = { game: (id) => `#/games/${id}`, user: () => "#/admin/users", signup: (id) => `#/signups/${id}`, job: (id) => `#/jobs/${id}`,
  apworld: () => "#/worlds", source: () => "#/worlds", config: (id) => `#/configs/${id}` };

function details(d) {
  if (!d) return null;
  const parts = Object.entries(d).map(([key, value]) => {
    const shown = key === "size" ? fmtBytes(value) : Array.isArray(value) ? value.join(", ") : value === true ? t("yes") : value === false ? t("no") : String(value);
    return `${(DETAIL_KEYS[key] || (() => key))()}: ${shown}`;
  });
  return h("span", { class: "muted small mono" }, parts.join(" · "));
}

export default async function render({ root, isCurrent }) {
  const users = await get("/api/users").catch(() => []);
  const who = select([["", t("Everyone")], ...users.map((u) => [u.id, u.username])], "");
  const area = select(AREAS.map(([key, label]) => [key, label()]), "");
  const host = h("div");
  const more = h("button", { class: "btn", type: "button", hidden: true }, t("Show older entries"));
  let rows = [];

  async function load(older = false) {
    const params = new URLSearchParams();
    if (older && rows.length) params.set("before", rows.at(-1).id);
    if (who.value) params.set("user_id", who.value);
    if (area.value) params.set("action", area.value);
    const page = await get(`/api/audit?${params}`);
    if (!isCurrent()) return;
    rows = older ? [...rows, ...page.entries] : page.entries;
    more.hidden = !page.more;
    draw();
  }

  function draw() {
    if (!rows.length) {
      clear(host).append(empty(t("Nothing recorded yet"), t("Changes administrators make, and every start and stop of a game, are listed here.")));
      return;
    }
    clear(host).append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("When"), t("Who"), t("What"), t("On"), t("Details")].map((text) => h("th", {}, text)))),
      h("tbody", {}, rows.map((e) => h("tr", {},
        h("td", { class: "muted", title: fmtDate(e.at) }, fmtAgo(e.at)),
        h("td", {}, h("span", { class: "slot-name" }, e.username)),
        h("td", {}, (ACTIONS[e.action] || (() => e.action))()),
        h("td", {}, e.target ? h("a", { href: LINKS[e.target.kind]?.(e.target.id) || "#/" }, e.target.name || `#${e.target.id}`) : h("span", { class: "muted" }, "—")),
        h("td", {}, details(e.details))))))));
  }

  who.onchange = () => load();
  area.onchange = () => load();
  more.onclick = () => load(true);
  root.append(pageHeader(t("Administration"), t("Server-wide settings.")), adminTabs("log"),
    h("div", { class: "stack" },
      h("div", { class: "row" }, field(t("Who"), who), field(t("Area"), area)),
      host, h("div", { class: "row center" }, more)));
  await load();
}
