// Reusable pieces shared by several views.
import { h, icon, copyText, openModal, toast, action, fmtBytes, fmtAgo } from "./util.js";
import { api, post, state, isAdmin } from "./api.js";

const STATE_LABELS = {
  running: ["ok", "Running"], starting: ["warn", "Starting…"], stopping: ["warn", "Stopping…"],
  stopped: ["muted", "Stopped"], crashed: ["bad", "Crashed"],
};

export function badge(text, kind = "muted") {
  return h("span", { class: `badge ${kind}` }, text);
}

export function stateBadge(stateName) {
  const [kind, label] = STATE_LABELS[stateName] || ["muted", stateName];
  return h("span", { class: `badge ${kind}` }, h("span", { class: `dot ${kind}` }), label);
}

export function jobBadge(status) {
  const map = { queued: ["muted", "Queued"], running: ["warn", "Generating…"], succeeded: ["ok", "Done"],
                failed: ["bad", "Failed"], cancelled: ["muted", "Cancelled"] };
  const [kind, label] = map[status] || ["muted", status];
  return h("span", { class: `badge ${kind}` }, label);
}

export function gameChip(name, missing = false) {
  return h("span", { class: `chip ${missing ? "missing" : ""}`, title: missing ? "Not installed on this server" : undefined },
    missing ? icon("alert", 12) : null, name);
}

export function chips(names, missing = []) {
  if (!names.length) return h("span", { class: "muted" }, "—");
  return h("span", { class: "chips" }, names.map((n) => gameChip(n, missing.includes(n))));
}

export function progressBar(value, total, { compact = false } = {}) {
  const pct = total ? Math.min(100, (value / total) * 100) : 0;
  return h("div", { class: `progress-wrap ${compact ? "compact" : ""}` },
    h("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": total, "aria-valuenow": value },
      h("div", { class: `bar ${pct >= 100 ? "done" : ""}`, style: { width: `${pct}%` } })),
    h("span", { class: "progress-label" }, total ? `${value} / ${total}` : "—"));
}

export function copyButton(text, label = "Copied") {
  return h("button", { class: "btn ghost icon-only sm", type: "button", title: "Copy", "aria-label": "Copy",
    onclick: () => copyText(text, label) }, icon("copy", 14));
}

export function copyField(text) {
  return h("div", { class: "copyfield" }, h("code", { class: "mono" }, text), copyButton(text));
}

export function pageHeader(title, subtitle, ...actions) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
    h("div", { class: "page-actions" }, actions));
}

export function empty(title, text, ...actions) {
  return h("div", { class: "empty" }, h("h3", {}, title), text ? h("p", { class: "muted" }, text) : null,
    actions.length ? h("div", { class: "row center" }, actions) : null);
}

export function loading(text = "Loading…") {
  return h("div", { class: "loading" }, h("span", { class: "spinner" }), text);
}

export function banner(kind, ...children) {
  return h("div", { class: `banner ${kind}`, role: kind === "error" ? "alert" : undefined }, ...children);
}

export function field(label, control, hint) {
  return h("label", { class: "field" }, h("span", { class: "field-label" }, label), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

export function checkbox(label, checked, hint) {
  const input = h("input", { type: "checkbox", checked: !!checked });
  const el = h("label", { class: "check" }, input, h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null));
  el.input = input;
  return el;
}

export function select(options, value) {
  const el = h("select", { class: "input" }, options.map(([val, text]) =>
    h("option", { value: String(val), selected: String(val) === String(value) }, text)));
  return el;
}

// ---------------------------------------------------------------------------------------------------------------------
// custom games (APWorlds)
// ---------------------------------------------------------------------------------------------------------------------
export function canSubmitApworld() {
  return isAdmin() || !!state.status?.permissions?.upload_apworlds;
}

export function uploadApworldModal({ game, onDone } = {}) {
  const admin = isAdmin();
  const input = h("input", { type: "file", accept: ".apworld", class: "input" });
  const status = h("div", { class: "muted" });
  const submit = h("button", { class: "btn primary", type: "button" },
    icon("upload"), admin ? "Install" : "Submit for approval");
  submit.onclick = action(async () => {
    const file = input.files[0];
    if (!file) throw new Error("Choose an .apworld file first");
    status.textContent = "Uploading…";
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api("POST", "/api/apworlds", form);
      modal.close();
      const verb = result.status === "installed" ? "installed" : "submitted for approval";
      toast(`${result.game} ${verb}`, "success");
      if (result.load_error) toast(`It did not load: ${result.load_error}`, "error");
      if (game && result.game !== game) toast(`Note: that file is for "${result.game}", not "${game}".`, "info");
      onDone?.(result);
    } finally {
      status.textContent = "";
    }
  });
  const modal = openModal({
    title: game ? `Add the game "${game}"` : "Add a custom game",
    body: h("div", { class: "stack" },
      h("p", {}, admin
        ? "Choose an .apworld file. It is installed immediately and available for generating."
        : "Choose an .apworld file. An administrator has to approve it before it can be used."),
      h("p", { class: "muted" }, "APWorlds contain code that runs on the server. Only add files from sources you trust."),
      input, status),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), submit],
  });
  return modal;
}

// Lists games that configs or hosted seeds use but the server does not have. `onChange` runs after an upload.
export function missingGamesPanel(items, onChange) {
  if (!items.length) return null;
  const admin = isAdmin();
  return h("div", { class: "stack missing-panel" }, items.map((item) => {
    const usedBy = [
      ...item.configs.map((c) => `config ${c.name}${admin && c.owner ? ` (${c.owner})` : ""}`),
      ...item.games.map((g) => `game ${g.name}`),
    ];
    const mine = item.pending.find((p) => p.id);
    const someonePending = item.pending.length > 0;
    return banner("warn",
      icon("alert", 18),
      h("div", { class: "grow" },
        h("strong", {}, item.game), " is not installed on this server.",
        usedBy.length ? h("div", { class: "muted small" }, "Used by ", usedBy.slice(0, 4).join(", "),
          usedBy.length > 4 ? `, and ${usedBy.length - 4} more` : "", ".") : null,
        item.games.length ? h("div", { class: "muted small" },
          "The server can still host this seed. Players need the APWorld to patch and play it.") : null),
      someonePending && (admin || mine)
        ? h("a", { class: "btn sm", href: "#/worlds" }, admin ? "Review submission" : "Waiting for approval")
        : someonePending ? h("span", { class: "muted small" }, "Waiting for approval")
        : canSubmitApworld()
          ? h("button", { class: "btn sm primary", type: "button",
              onclick: () => uploadApworldModal({ game: item.game, onDone: onChange }) },
            icon("upload", 14), admin ? "Add APWorld" : "Submit APWorld")
          : h("span", { class: "muted small" }, "Ask an administrator to add it"));
  }));
}

// Shown right after importing a seed or uploading a yaml that needs games the server does not have.
// `items` look like the entries of /api/missing-games: {game, configs?, games?}.
export function promptMissingGames(items, onChange) {
  if (!items?.length) return;
  const full = items.map((item) => ({ configs: [], games: [], pending: [], ...item }));
  const body = h("div", { class: "stack" },
    h("p", {}, full.length === 1 ? "This game is not installed on the server:" : "These games are not installed on the server:"),
    missingGamesPanel(full, () => { modal.close(); onChange?.(); }));
  const modal = openModal({ title: "Custom game needed", body,
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Not now")] });
}

// ---------------------------------------------------------------------------------------------------------------------
// games
// ---------------------------------------------------------------------------------------------------------------------
export function isActive(game) {
  return game.state === "running" || game.state === "starting";
}

// Start / stop / restart for whoever may control the game. `onUpdate` receives the refreshed game.
export function controlButtons(game, onUpdate) {
  if (!game.can_control) return null;
  const run = (label, iconName, verb, cls = "") => h("button", {
    class: `btn sm ${cls}`, type: "button",
    onclick: action(async () => onUpdate(await post(`/api/games/${game.id}/${verb}`))),
  }, icon(iconName, 14), label);
  return isActive(game)
    ? [run("Restart", "restart", "restart"), run("Stop", "stop", "stop")]
    : [run("Start", "play", "start", "primary")];
}

export function gameCard(game, onUpdate) {
  return h("div", { class: "card game-card" },
    h("div", { class: "card-head" },
      h("div", { class: "stack tight" },
        h("h3", {}, h("a", { href: `#/games/${game.id}` }, game.name)),
        chips(game.games, game.missing_games)),
      h("div", { class: "row" }, game.race_mode ? badge("Race", "warn") : null, stateBadge(game.state))),
    game.error && game.state === "crashed" ? h("div", { class: "form-error" }, game.error) : null,
    game.address ? h("div", { class: "row" }, h("span", { class: "muted small" }, "Server"), copyField(game.address)) : null,
    game.my_slots.length
      ? h("div", { class: "stack tight" }, h("span", { class: "muted small" }, game.my_slots.length > 1 ? "Your slots" : "Your slot"),
        game.my_slots.map((s) => h("div", {}, h("span", { class: "slot-name" }, s.name), h("span", { class: "muted" }, ` · ${s.game}`))))
      : h("div", { class: "muted small" }, `${game.claimed_count} of ${game.player_count} slots have an owner`),
    h("div", { class: "row" },
      h("a", { class: "btn sm", href: `#/games/${game.id}` }, "Open"),
      game.my_file_count ? h("a", { class: "btn sm", href: `/api/games/${game.id}/my-files.zip` }, icon("download", 14),
        game.my_file_count > 1 ? `My ${game.my_file_count} files` : "My file") : null,
      controlButtons(game, onUpdate)));
}

// ---------------------------------------------------------------------------------------------------------------------
// Archipelago server rules, used for per-game settings and for the global defaults
// ---------------------------------------------------------------------------------------------------------------------
const RELEASE_CHOICES = [["auto", "auto (on goal)"], ["enabled", "enabled (always)"], ["disabled", "disabled"],
  ["goal", "goal (after goal)"], ["auto-enabled", "auto + manual"]];
const REMAINING_CHOICES = [["goal", "goal (after goal)"], ["enabled", "enabled (always)"], ["disabled", "disabled"]];
const COUNTDOWN_CHOICES = [["auto", "auto"], ["enabled", "enabled"], ["disabled", "disabled"]];

export function serverOptionsForm(opts) {
  const number = (key, min, max) => h("input", { class: "input", type: "number", min, max, value: opts[key] });
  const o = {
    hint_cost: number("hint_cost", 0, 100), location_check_points: number("location_check_points", 0, 1000),
    auto_shutdown_minutes: number("auto_shutdown_minutes", 0, 43200),
    release_mode: select(RELEASE_CHOICES, opts.release_mode), collect_mode: select(RELEASE_CHOICES, opts.collect_mode),
    remaining_mode: select(REMAINING_CHOICES, opts.remaining_mode), countdown_mode: select(COUNTDOWN_CHOICES, opts.countdown_mode),
    compatibility: select([[2, "Casual: any client version"], [0, "Strict: exact version only"]], opts.compatibility),
    loglevel: select(["info", "debug", "warning", "error"].map((v) => [v, v]), opts.loglevel),
  };
  const noCheat = checkbox("Disable !getitem (item cheat)", opts.disable_item_cheat);
  const embedded = checkbox("Use the hint/release options stored in the seed", opts.use_embedded_options,
    "Seeds created on archipelago.gg carry the options chosen there.");
  const node = h("div", { class: "stack" },
    h("div", { class: "form-grid" },
      field("Hint cost (%)", o.hint_cost, "Share of a player's locations that earns one hint"),
      field("Hint points per check", o.location_check_points),
      field("!release", o.release_mode), field("!collect", o.collect_mode),
      field("!remaining", o.remaining_mode), field("!countdown", o.countdown_mode),
      field("Client compatibility", o.compatibility),
      field("Idle shutdown (minutes)", o.auto_shutdown_minutes, "Stops the server after this long without a new check. 0 = never. Takes effect on restart."),
      field("Log level", o.loglevel)),
    noCheat, embedded);
  const read = () => ({
    hint_cost: Number(o.hint_cost.value), location_check_points: Number(o.location_check_points.value),
    auto_shutdown_minutes: Number(o.auto_shutdown_minutes.value),
    release_mode: o.release_mode.value, collect_mode: o.collect_mode.value, remaining_mode: o.remaining_mode.value,
    countdown_mode: o.countdown_mode.value, compatibility: Number(o.compatibility.value), loglevel: o.loglevel.value,
    disable_item_cheat: noCheat.input.checked, use_embedded_options: embedded.input.checked,
  });
  return { node, read };
}

export function adminTabs(current) {
  return h("div", { class: "tabs", role: "navigation" },
    [["users", "Users"], ["settings", "Settings"]].map(([key, label]) =>
      h("a", { class: "tab", href: `#/admin/${key}`, "aria-current": key === current ? "page" : undefined }, label)));
}

// ---------------------------------------------------------------------------------------------------------------------
// Archipelago updates (admins only: they come from /api/system)
// ---------------------------------------------------------------------------------------------------------------------
const README_UPDATING = "https://github.com/NiklaskoDev/ArchipelagoManagerDocker#updating";

export function updateBanner(update) {
  if (!update?.update_available) return null;
  return banner("info", icon("package", 18), h("div", { class: "grow" },
    h("strong", {}, `Archipelago ${update.latest} is available`), ` (this server runs ${update.installed}).`,
    h("div", { class: "small" }, "Update the container image to get it: on Unraid, Docker → Check for updates → Apply. ",
      "Users, configs and games are kept, and running games start again."),
    h("div", { class: "small" },
      update.url ? [h("a", { href: update.url, target: "_blank", rel: "noopener" }, "Release notes"), " · "] : null,
      h("a", { href: README_UPDATING, target: "_blank", rel: "noopener" }, "No newer image offered yet?"))));
}

export function updateStatus(update) {
  if (!update.enabled) return h("span", { class: "muted" }, "Not checked: turned off with UPDATE_CHECK=false");
  if (update.update_available) {
    return h("span", { class: "row" }, badge(`${update.latest} available`, "warn"),
      update.url ? h("a", { href: update.url, target: "_blank", rel: "noopener" }, "release notes") : null);
  }
  if (update.latest) {
    return h("span", { class: "row" }, badge("Up to date", "ok"),
      h("span", { class: "muted small" }, `latest release ${update.latest}, checked ${fmtAgo(update.checked_at)}`));
  }
  if (update.error) return h("span", { class: "muted", title: update.error }, "Could not reach GitHub to check");
  return h("span", { class: "muted" }, "Not checked yet");
}
