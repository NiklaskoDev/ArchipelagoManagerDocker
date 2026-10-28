// Reusable pieces shared by several views.
import { h, clear, icon, copyText, openModal, toast, action, confirmDialog, fmtBytes, fmtAgo, fmtDate, fmtUntil, withNodes } from "./util.js";
import { api, get, post, put, del, state, isAdmin, isOrganizer } from "./api.js";
import { t, tServer, plural } from "./i18n.js";

const STATE_LABELS = {
  running: ["ok", t("Running")], starting: ["warn", t("Starting…")], stopping: ["warn", t("Stopping…")],
  stopped: ["muted", t("Stopped")], crashed: ["bad", t("Crashed")], sleeping: ["muted", t("Asleep")],
};

const JOB_LABELS = {
  queued: ["muted", t("Queued")], running: ["warn", t("Generating…")], succeeded: ["ok", t("Done")],
  failed: ["bad", t("Failed")], cancelled: ["muted", t("Cancelled")],
};

export function badge(text, kind = "muted") {
  return h("span", { class: `badge ${kind}` }, text);
}

export function stateBadge(stateName) {
  const [kind, label] = STATE_LABELS[stateName] || ["muted", stateName];
  return h("span", { class: `badge ${kind}` }, h("span", { class: `dot ${kind}` }), label);
}

export function jobBadge(status) {
  const [kind, label] = JOB_LABELS[status] || ["muted", status];
  return h("span", { class: `badge ${kind}` }, label);
}

export function gameChip(name, missing = false) {
  return h("span", { class: `chip ${missing ? "missing" : ""}`, title: missing ? t("Not installed on this server") : undefined },
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

export function copyButton(text, label = t("Copied")) {
  return h("button", { class: "btn ghost icon-only sm", type: "button", title: t("Copy"), "aria-label": t("Copy"),
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

export function loading(text = t("Loading…")) {
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

// How the last Discord message to a webhook went: an error after the last success, or when one was last delivered
export function deliveryStatus(status) {
  if (!status) return null;
  if (status.last_error && (!status.last_ok_at || status.last_error_at > status.last_ok_at)) {
    return banner("error", icon("alert", 16), h("div", { class: "grow" },
      h("strong", {}, t("A message could not be sent ({ago}):", { ago: fmtAgo(status.last_error_at) })), " ", tServer(status.last_error)));
  }
  return status.last_ok_at ? h("p", { class: "muted small" }, t("Last message delivered {ago}.", { ago: fmtAgo(status.last_ok_at) })) : null;
}

// A player name to start a new config with: the user's name, as far as Archipelago allows it
export function defaultPlayerName() {
  return state.user.username.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 16) || "Player";
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
    icon("upload"), admin ? t("Install") : t("Submit for approval"));
  submit.onclick = action(async () => {
    const file = input.files[0];
    if (!file) throw new Error(t("Choose an .apworld file first"));
    status.textContent = t("Uploading…");
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api("POST", "/api/apworlds", form);
      modal.close();
      toast(result.status === "installed" ? t("{name} installed", { name: result.game })
        : t("{name} submitted for approval", { name: result.game }), "success");
      if (result.load_error) toast(t("It did not load: {error}", { error: tServer(result.load_error) }), "error");
      if (game && result.game !== game) toast(t("Note: that file is for \"{actual}\", not \"{game}\".", { actual: result.game, game }), "info");
      onDone?.(result);
    } finally {
      status.textContent = "";
    }
  });
  const modal = openModal({
    title: game ? t("Add the game \"{game}\"", { game }) : t("Add a custom game"),
    body: h("div", { class: "stack" },
      h("p", {}, admin
        ? t("Choose an .apworld file. It is installed immediately and available for generating.")
        : t("Choose an .apworld file. An administrator has to approve it before it can be used.")),
      h("p", { class: "muted" }, t("APWorlds contain code that runs on the server. Only add files from sources you trust.")),
      input, status,
      h("p", { class: "muted small" }, t("Published on GitHub?"), " ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); modal.close(); githubSourceModal({ onDone }); } },
          t("Add it from its repository instead")), t(", so new releases show up here."))),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), submit],
  });
  return modal;
}

// Adds a GitHub repository as the source of a custom game. A player's repository waits for an administrator; nothing
// is downloaded from it before that.
export function githubSourceModal({ onDone } = {}) {
  const admin = isAdmin();
  const repo = h("input", { class: "input", placeholder: t("owner/name or https://github.com/owner/name"), autocomplete: "off" });
  const note = h("input", { class: "input", maxLength: 300, placeholder: t("e.g. Pokemon Crystal, for Friday's round") });
  const found = h("div", { class: "stack tight" });
  const submit = h("button", { class: "btn primary", type: "button", disabled: true },
    icon(admin ? "download" : "upload", 14), admin ? t("Install") : t("Submit for approval"));
  let release = null, assetSelect = null;

  const lookUp = action(async () => {
    release = null;
    submit.disabled = true;
    clear(found).append(h("span", { class: "muted small" }, t("Looking it up on GitHub…")));
    try {
      release = await get(`/api/apworld-sources/lookup?repo=${encodeURIComponent(repo.value.trim())}`);
    } catch (err) {
      clear(found).append(banner("error", icon("alert", 16), err.message));
      return;
    }
    assetSelect = release.assets.length > 1 ? select(release.assets.map((a) => [a.name, `${a.name} (${fmtBytes(a.size)})`]), release.assets[0].name) : null;
    clear(found).append(
      h("div", {}, h("a", { href: `https://github.com/${release.repo}`, target: "_blank", rel: "noopener" }, h("strong", {}, release.repo)),
        release.description ? h("div", { class: "muted small" }, release.description) : null),
      h("div", { class: "small" }, t("Newest release:"), " ", h("a", { href: release.release_url, target: "_blank", rel: "noopener" }, release.tag)),
      !release.assets.length ? banner("error", icon("alert", 16), t("This release has no .apworld file."))
        : assetSelect ? field(t("File to install"), assetSelect, t("The release has several .apworld files."))
        : h("div", { class: "small" }, t("File:"), " ", h("code", {}, release.assets[0].name)),
      release.existing ? banner("warn", icon("alert", 16), release.existing.status === "pending"
        ? t("This repository was already submitted and waits for approval.") : t("This repository was already added.")) : null);
    submit.disabled = !release.assets.length || !!release.existing;
  });
  const lookButton = h("button", { class: "btn", type: "button", onclick: lookUp }, t("Look up"));
  repo.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); lookButton.click(); } });
  repo.addEventListener("input", () => { release = null; submit.disabled = true; clear(found); });

  submit.onclick = action(async () => {
    const result = await post("/api/apworld-sources", {
      repo: release.repo, asset: assetSelect ? assetSelect.value : release.assets[0].name, note: note.value || null });
    modal.close();
    toast(admin ? t("{game} installed from {repo}", { game: result.game, repo: result.repo })
      : t("{name} submitted for approval", { name: result.repo }), "success");
    onDone?.(result);
  });
  const modal = openModal({
    title: t("Add a custom game from GitHub"),
    body: h("div", { class: "stack" },
      h("p", {}, admin
        ? t("The newest release's .apworld is installed now. New releases show up on this page; installing one is your call.")
        : t("An administrator has to approve the repository. Nothing is downloaded from it before that.")),
      h("p", { class: "muted small" }, t("APWorlds contain code that runs on the server. Only add repositories you trust.")),
      field(t("Repository"), h("div", { class: "row" }, h("div", { class: "grow" }, repo), lookButton)),
      found,
      admin ? null : field(t("Note for the administrator"), note)),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), submit],
  });
  return modal;
}

// Lists games that configs or hosted seeds use but the server does not have. `onChange` runs after an upload.
export function missingGamesPanel(items, onChange) {
  if (!items.length) return null;
  const admin = isAdmin();
  return h("div", { class: "stack missing-panel" }, items.map((item) => {
    const usedBy = [
      ...item.configs.map((c) => (admin && c.owner ? t("config {name} ({owner})", { name: c.name, owner: c.owner })
        : t("config {name}", { name: c.name }))),
      ...item.games.map((g) => t("game {name}", { name: g.name })),
    ];
    const shown = usedBy.slice(0, 4).join(", ");
    const mine = item.pending.find((p) => p.id);
    const someonePending = item.pending.length > 0;
    return banner("warn",
      icon("alert", 18),
      h("div", { class: "grow" },
        withNodes(t("{game} is not installed on this server."), { game: h("strong", {}, item.game) }),
        usedBy.length ? h("div", { class: "muted small" }, usedBy.length > 4
          ? t("Used by {list}, and {n} more.", { list: shown, n: usedBy.length - 4 }) : t("Used by {list}.", { list: shown })) : null,
        item.games.length ? h("div", { class: "muted small" },
          t("The server can still host this seed. Players need the APWorld to patch and play it.")) : null),
      someonePending && (admin || mine)
        ? h("a", { class: "btn sm", href: "#/worlds" }, admin ? t("Review submission") : t("Waiting for approval"))
        : someonePending ? h("span", { class: "muted small" }, t("Waiting for approval"))
        : canSubmitApworld()
          ? h("button", { class: "btn sm primary", type: "button",
              onclick: () => uploadApworldModal({ game: item.game, onDone: onChange }) },
            icon("upload", 14), admin ? t("Add APWorld") : t("Submit APWorld"))
          : h("span", { class: "muted small" }, t("Ask an administrator to add it")));
  }));
}

// Games that cannot be generated until an administrator puts their base ROM on the server. Takes missing_roms arrays
// from the API ({game, file, folder, in_data_dir}), nested as deep as convenient; each game is shown once.
export function missingRomsPanel(...lists) {
  const items = [...new Map(lists.flat(Infinity).map((item) => [item.game, item])).values()];
  if (!items.length) return null;
  const admin = isAdmin();
  return h("div", { class: "stack missing-panel" }, items.map((item) => {
    const note = banner("warn", icon("alert", 18), h("div", { class: "grow" },
      withNodes(t("{game} needs its base ROM on the server before it can be generated."), { game: h("strong", {}, item.game) }),
      admin
        ? h("div", { class: "small" }, t("Upload it here, or copy it to the server yourself:"), " ", withNodes(romPlace(item), {
          file: h("code", {}, item.file), folder: h("code", {}, item.folder === "." ? t("the data folder") : `${item.folder}/`) }))
        : h("div", { class: "small muted" }, t("An administrator has to add it to the server."))),
      admin ? h("button", { class: "btn sm", type: "button", onclick: () => romUploadModal(item, () => note.remove()) }, icon("upload", 14), t("Upload the ROM")) : null);
    return note;
  }));
}

// Uploading a base ROM. `rom` is an entry of /api/roms (or of a missing ROM): {game, setting, file, checked}.
// `onDone` receives the new list of ROMs.
export function romUploadModal(rom, onDone) {
  const input = h("input", { type: "file", hidden: true });
  const drop = h("div", { class: "dropzone", tabIndex: 0 }, t("Drop the ROM here, or click to choose it"));
  const setFile = (file) => {
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    drop.textContent = file.name;
  };
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); setFile(e.dataTransfer.files[0]); };
  input.onchange = () => setFile(input.files[0]);
  const note = h("div", { class: "muted small" });
  const submit = h("button", { class: "btn primary", type: "button" }, icon("upload", 14), t("Upload"));
  submit.onclick = action(async () => {
    if (!input.files[0]) throw new Error(t("Choose a file first"));
    const form = new FormData();
    form.append("game", rom.game);
    form.append("setting", rom.setting || "");
    form.append("file", input.files[0]);
    note.textContent = rom.checked === false ? t("Uploading…") : t("Uploading and checking the file…");
    try {
      const list = await api("POST", "/api/roms", form);
      modal.close();
      toast(t("The ROM for {game} is in place", { game: rom.game }), "success");
      onDone?.(list);
    } finally {
      note.textContent = "";
    }
  });
  const modal = openModal({
    title: t("Base ROM for {game}", { game: rom.game }),
    body: h("div", { class: "stack" },
      h("p", { class: "muted small", style: { margin: 0 } }, rom.checked === false
        ? t("{game} does not say which file it accepts. The file is stored as {file}, where the game looks for it.", { game: rom.game, file: rom.file })
        : t("The file is checked against the checksums {game} knows, so it has to be the region and revision its setup guide names. Its name does not matter: it is stored as {file}, where the game looks for it.", { game: rom.game, file: rom.file })),
      drop, input, note),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), submit],
  });
}

// Every base ROM the installed games ask for, for administrators: the ones generating needs first.
export function romsCard() {
  const host = h("div", { class: "stack" });
  const status = (rom) => {
    if (rom.exists && rom.valid === false) return badge(t("Wrong file"), "bad");
    if (rom.exists) return badge(t("In place"), "ok");
    return rom.needed ? badge(t("Missing"), "warn") : h("span", { class: "muted small" }, t("not on the server"));
  };
  const table = (list, draw) => h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, [t("Game"), t("File"), t("Status"), ""].map((text) => h("th", {}, text)))),
    h("tbody", {}, list.map((rom) => h("tr", {},
      h("td", {}, h("span", { class: "slot-name" }, rom.game), rom.description ? h("div", { class: "muted small" }, rom.description) : null),
      h("td", {}, h("span", { class: "mono small" }, rom.file), h("div", { class: "muted small" },
        rom.in_data_dir ? (rom.folder === "." ? t("in the data folder") : t("in {folder}/ in the data folder", { folder: rom.folder })) : `${rom.folder}/`)),
      h("td", {}, status(rom)),
      h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end", flexWrap: "nowrap" } },
        h("button", { class: "btn sm", type: "button", onclick: () => romUploadModal(rom, draw) }, icon("upload", 14), rom.exists ? t("Replace") : t("Upload")),
        rom.exists ? h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Remove"), "aria-label": t("Remove the ROM of {game}", { game: rom.game }),
          onclick: action(async () => {
            if (!(await confirmDialog(t("The ROM of {game} is deleted from the server.", { game: rom.game }), { title: t("Remove this ROM?"), danger: true, confirmText: t("Remove") }))) return;
            draw(await del(`/api/roms?game=${encodeURIComponent(rom.game)}&setting=${encodeURIComponent(rom.setting || "")}`));
          }) }, icon("trash", 14)) : null)))))));
  const draw = (list) => {
    const needed = list.filter((r) => r.needed), others = list.filter((r) => !r.needed);
    clear(host).append(
      needed.length ? table(needed, draw) : h("p", { class: "muted" }, t("None of the installed games needs a ROM to generate.")),
      others.length ? h("details", {}, h("summary", { class: "muted" }, t("Only needed by players to patch ({n})", { n: others.length })),
        h("p", { class: "muted small" }, t("Generating these games works without the ROM; each player patches with their own. Upload one only if the server should have it anyway.")),
        table(others, draw)) : null);
  };
  const load = () => get("/api/roms").then(draw).catch((err) => clear(host).append(banner("error", icon("alert", 16), err.message)));
  load();
  return h("div", { class: "card stack" }, h("h2", {}, t("Base ROMs")),
    h("p", { class: "muted small" }, t("Some games need their original ROM on the server to generate a multiworld. Upload it here: it is checked against the checksums the game knows and stored under the name the game looks for.")),
    host);
}

// Where the base ROM goes, with {file} and {folder} still in the text
function romPlace(item) {
  if (!item.in_data_dir) return t("Put {file} into {folder}.");
  return item.folder === "." ? t("Put {file} into {folder} (on Unraid usually /mnt/user/appdata/archipelago-manager).")
    : t("Put {file} into {folder} in the data folder (on Unraid usually /mnt/user/appdata/archipelago-manager).");
}

// Shown right after importing a seed or uploading a yaml that needs games the server does not have.
// `items` look like the entries of /api/missing-games: {game, configs?, games?}.
export function promptMissingGames(items, onChange) {
  if (!items?.length) return;
  const full = items.map((item) => ({ configs: [], games: [], pending: [], ...item }));
  const body = h("div", { class: "stack" },
    h("p", {}, plural(full.length, "This game is not installed on the server:", "These games are not installed on the server:")),
    missingGamesPanel(full, () => { modal.close(); onChange?.(); }));
  const modal = openModal({ title: t("Custom game needed"), body,
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Not now"))] });
}

// ---------------------------------------------------------------------------------------------------------------------
// checking a config (POST /api/configs/check, configcheck.py)
// ---------------------------------------------------------------------------------------------------------------------
// Whether the generator takes a config, as Archipelago reads it. Opens at once and fills in when Archipelago answers,
// which takes a few seconds. `load` gives the file's text; `editHref` adds a way to the editor.
export function configCheckDialog({ name, load, editHref = null }) {
  const body = h("div", { class: "stack" }, loading(t("Archipelago is reading the file…")));
  const modal = openModal({ title: t("Check {name}", { name }), wide: true, body,
    actions: [editHref ? h("a", { class: "btn", href: editHref, onclick: () => modal.close() }, icon("edit", 14), t("Edit")) : null,
      h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close"))].filter(Boolean) });
  (async () => {
    try {
      drawConfigCheck(body, await post("/api/configs/check", { content: await load() }));
    } catch (err) {
      clear(body).append(banner("error", icon("alert", 16), h("div", { class: "grow" }, err.message)));
    }
  })();
}

function drawConfigCheck(body, r) {
  // the manager's own messages are translated; Archipelago's stay in its words
  const list = (items, kind) => (items.length
    ? h("ul", { class: `check-list ${kind}` }, items.map((text) => h("li", {}, tServer(text)))) : null);
  const fileWarnings = [...r.warnings, ...r.missing_roms.map((game) => t("{game} needs its base ROM on the server before it can be generated.", { game }))];
  const warned = fileWarnings.length || r.players.some((p) => p.warnings.length);
  clear(body).append(
    r.ok ? banner("ok", icon("check", 18), h("div", { class: "grow" }, h("strong", {}, t("Archipelago takes this config.")),
      warned ? ` ${t("Have a look at the warnings, though.")}` : null))
      : banner("error", icon("alert", 18), h("div", { class: "grow" }, h("strong", {}, t("The generator would stop at this config.")),
        " ", t("Fix what is listed below, then check again."))),
    list(r.errors, "bad"), list(fileWarnings, "warn"),
    r.players.map((p) => h("div", { class: "stack tight" },
      h("div", { class: "row" }, h("strong", { class: "slot-name" }, p.name), h("span", { class: "muted" }, p.games.join(", ")),
        p.errors.length ? badge(plural(p.errors.length, "{n} error", "{n} errors"), "bad")
          : p.warnings.length ? badge(plural(p.warnings.length, "{n} warning", "{n} warnings"), "warn") : badge(t("Fine"), "ok")),
      list(p.errors, "bad"), list(p.warnings, "warn"))),
    h("p", { class: "muted small" }, t("Archipelago read the file the way the generator does, with every value a weight can pick and every game the file can roll. Nothing was generated, though: whether the items can be placed with these options only shows when a game is generated.")));
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
    ? [run(t("Restart"), "restart", "restart"), run(t("Stop"), "stop", "stop")]
    : [run(t("Start"), "play", "start", "primary")];
}

// Which Archipelago version generates or hosts a game (GET /api/ap-versions): null when there is only the server's own
export function apVersionField(listing, value = "") {
  const installed = (listing?.versions || []).filter((v) => v.status === "installed");
  if (!installed.length) return null;
  const choice = select([["", t("This server's ({version})", { version: listing.default || "?" })],
    ...installed.map((v) => [v.version, v.version])], installed.some((v) => v.version === value) ? value : "");
  // set(version): picks it if it is installed; returns whether it is
  const set = (version) => {
    const ok = !version || installed.some((v) => v.version === version);
    choice.value = ok ? version || "" : "";
    return ok;
  };
  return { node: field(t("Archipelago version"), choice, t("The game runs on it, and players need a client of it.")), read: () => choice.value, set };
}

export function gameCard(game, onUpdate) {
  return h("div", { class: "card game-card" },
    h("div", { class: "card-head" },
      h("div", { class: "stack tight" },
        h("h3", {}, h("a", { href: `#/games/${game.id}` }, game.name)),
        chips(game.games, game.missing_games)),
      h("div", { class: "row" }, game.race_mode ? badge(t("Race"), "warn") : null, stateBadge(game.state))),
    game.error && game.state === "crashed" ? h("div", { class: "form-error" }, tServer(game.error)) : null,
    game.start && game.start.stage !== "started" ? h("div", { class: "row small" }, icon("calendar", 14),
      t("Starts {date}", { date: fmtDate(game.start.at) }), h("span", { class: "muted" }, fmtUntil(game.start.at))) : null,
    game.address ? h("div", { class: "row" }, h("span", { class: "muted small" }, t("Server")), copyField(game.address)) : null,
    game.my_slots.length
      ? h("div", { class: "stack tight" }, h("span", { class: "muted small" }, plural(game.my_slots.length, "Your slot", "Your slots")),
        game.my_slots.map((s) => h("div", {}, h("span", { class: "slot-name" }, s.name), h("span", { class: "muted" }, ` · ${s.game}`))))
      : h("div", { class: "muted small" }, t("{claimed} of {total} slots have an owner", { claimed: game.claimed_count, total: game.player_count })),
    h("div", { class: "row" },
      h("a", { class: "btn sm", href: `#/games/${game.id}` }, t("Open")),
      game.my_file_count ? h("a", { class: "btn sm", href: `/api/games/${game.id}/my-files.zip` }, icon("download", 14),
        plural(game.my_file_count, "My file", "My {n} files")) : null,
      controlButtons(game, onUpdate)));
}

// ---------------------------------------------------------------------------------------------------------------------
// Archipelago server rules, used for per-game settings and for the global defaults
// ---------------------------------------------------------------------------------------------------------------------
const RELEASE_CHOICES = [["auto", t("auto (on goal)")], ["enabled", t("enabled (always)")], ["disabled", t("disabled (never)")],
  ["goal", t("goal (after goal)")], ["auto-enabled", t("auto + manual")]];
const REMAINING_CHOICES = [["goal", t("goal (after goal)")], ["enabled", t("enabled (always)")], ["disabled", t("disabled (never)")]];
const COUNTDOWN_CHOICES = [["auto", t("auto")], ["enabled", t("enabled")], ["disabled", t("disabled (never)")]];

export function serverOptionsForm(opts) {
  const number = (key, min, max) => h("input", { class: "input", type: "number", min, max, value: opts[key] });
  const o = {
    hint_cost: number("hint_cost", 0, 100), location_check_points: number("location_check_points", 0, 1000),
    auto_shutdown_minutes: number("auto_shutdown_minutes", 1, 43200),
    release_mode: select(RELEASE_CHOICES, opts.release_mode), collect_mode: select(RELEASE_CHOICES, opts.collect_mode),
    remaining_mode: select(REMAINING_CHOICES, opts.remaining_mode), countdown_mode: select(COUNTDOWN_CHOICES, opts.countdown_mode),
    compatibility: select([[2, t("Casual: any client version")], [0, t("Strict: exact version only")]], opts.compatibility),
    loglevel: select(["info", "debug", "warning", "error"].map((v) => [v, v]), opts.loglevel),
  };
  // asleep, a game's server is stopped until a player opens the game page; 0 minutes = never
  const awake = checkbox(t("Never falls asleep"), !opts.auto_shutdown_minutes, t("The server keeps running until it is stopped."));
  const syncSleep = () => {
    o.auto_shutdown_minutes.disabled = awake.input.checked;
    if (!awake.input.checked && !Number(o.auto_shutdown_minutes.value)) o.auto_shutdown_minutes.value = 120;
  };
  awake.input.onchange = syncSleep;
  syncSleep();
  const noCheat = checkbox(t("Disable !getitem (item cheat)"), opts.disable_item_cheat);
  const embedded = checkbox(t("Use the hint/release options stored in the seed"), opts.use_embedded_options,
    t("Seeds created on archipelago.gg carry the options chosen there."));
  const tls = checkbox(t("Encrypted connections (wss)"), opts.tls,
    t("When a certificate is set under Admin → Settings. Turn it off for a game whose clients cannot encrypt; a running server changes over once nobody is connected."));
  const node = h("div", { class: "stack" },
    h("div", { class: "form-grid" },
      field(t("Hint cost (%)"), o.hint_cost, t("Share of a player's locations that earns one hint")),
      field(t("Hint points per check"), o.location_check_points),
      field("!release", o.release_mode), field("!collect", o.collect_mode),
      field("!remaining", o.remaining_mode), field("!countdown", o.countdown_mode),
      field(t("Client compatibility"), o.compatibility),
      field(t("Falls asleep after (minutes)"), o.auto_shutdown_minutes,
        t("With nobody connected. A player wakes it up again by opening the game page.")),
      field(t("Log level"), o.loglevel)),
    awake, noCheat, embedded, tls);
  const read = () => ({
    hint_cost: Number(o.hint_cost.value), location_check_points: Number(o.location_check_points.value),
    auto_shutdown_minutes: awake.input.checked ? 0 : Number(o.auto_shutdown_minutes.value),
    release_mode: o.release_mode.value, collect_mode: o.collect_mode.value, remaining_mode: o.remaining_mode.value,
    countdown_mode: o.countdown_mode.value, compatibility: Number(o.compatibility.value), loglevel: o.loglevel.value,
    disable_item_cheat: noCheat.input.checked, use_embedded_options: embedded.input.checked, tls: tls.input.checked,
  });
  return { node, read };
}

// ---------------------------------------------------------------------------------------------------------------------
// templates of generation options (presets.py)
// ---------------------------------------------------------------------------------------------------------------------
// The server settings of the game a generation makes, for organizers and administrators: the defaults for new games,
// or its own. `defaults` are the server's defaults (from /api/presets).
export function gameSettingsSection(own, defaults) {
  const formHost = h("div");
  let form = null;
  const ownBox = checkbox(t("Own settings for this game"), !!own, t("Otherwise it gets the defaults for new games, as set under Admin → Settings."));
  const draw = (opts) => {
    form = serverOptionsForm({ ...defaults, ...(opts || {}) });
    clear(formHost).append(form.node);
  };
  const sync = () => { formHost.hidden = !ownBox.input.checked; };
  ownBox.input.onchange = sync;
  draw(own);
  sync();
  const node = h("details", { open: !!own }, h("summary", { class: "muted" }, t("Settings for the new game")),
    h("div", { class: "stack", style: { marginTop: "10px" } }, ownBox, formHost));
  return {
    node,
    read: () => (ownBox.input.checked ? form.read() : null),
    apply: (opts) => { ownBox.input.checked = !!opts; draw(opts); sync(); if (opts) node.open = true; },
  };
}

// Pick a template to fill a form in, or save what the form holds as one. `read()` returns the form's options as a
// template has them; `apply(options)` fills the form in and returns what it had to leave out, as text, or nothing.
export function presetBar({ read, apply }) {
  const choice = h("select", { class: "input", "aria-label": t("Template") });
  const save = h("button", { class: "btn sm", type: "button" }, icon("plus", 14), t("Save as template"));
  const update = h("button", { class: "btn sm", type: "button", title: t("Save the options in the form into this template") }, t("Update"));
  const remove = h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Delete template"), "aria-label": t("Delete template") }, icon("trash", 14));
  let list = [];
  const current = () => list.find((p) => String(p.id) === choice.value);
  const sync = () => { update.hidden = remove.hidden = !current()?.can_edit; };
  const label = (p) => p.name + (p.shared ? ` · ${p.mine ? t("shared") : t("from {owner}", { owner: p.owner || "—" })}` : "");
  const draw = (selected = "") => {
    clear(choice).append(h("option", { value: "" }, t("Fill in from a template…")),
      list.map((p) => h("option", { value: String(p.id), selected: String(p.id) === String(selected) }, label(p))));
    sync();
  };
  const store = (saved) => {
    list = [...list.filter((p) => p.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    draw(saved.id);
  };

  choice.onchange = () => {
    sync();
    const preset = current();
    if (!preset) return;
    const leftOut = apply(preset.options);
    toast(leftOut ? `${t("Filled in from {name}.", { name: preset.name })} ${leftOut}` : t("Filled in from {name}.", { name: preset.name }),
      leftOut ? "info" : "success", leftOut ? 8000 : 4500);
  };
  update.onclick = action(async () => {
    const preset = current();
    if (!(await confirmDialog(t("The options in the form replace the ones saved in it."), { title: t("Update {name}?", { name: preset.name }), confirmText: t("Update") }))) return;
    store(await put(`/api/presets/${preset.id}`, { options: read() }));
    toast(t("Template {name} updated", { name: preset.name }), "success");
  });
  remove.onclick = action(async () => {
    const preset = current();
    if (!(await confirmDialog(preset.shared ? t("Nobody can fill a form in from it any more.") : t("It cannot be brought back."),
      { title: t("Delete {name}?", { name: preset.name }), danger: true, confirmText: t("Delete") }))) return;
    await del(`/api/presets/${preset.id}`);
    list = list.filter((p) => p.id !== preset.id);
    draw();
    toast(t("Template {name} deleted", { name: preset.name }), "success");
  });
  save.onclick = () => {
    const name = h("input", { class: "input", maxLength: 60, placeholder: t("e.g. Race or Async"), value: current()?.mine ? current().name : "" });
    const shared = isOrganizer() ? checkbox(t("Share with everyone who generates"), !!current()?.shared,
      t("They can fill their forms in from it; only you and administrators change it.")) : null;
    const submit = h("button", { class: "btn primary", type: "button" }, t("Save"));
    const modal = openModal({ title: t("Save as template"),
      body: h("div", { class: "stack" },
        h("p", { class: "muted small" }, t("Saves the options in this form under a name, to fill them in again later. The players and the seed are not part of it.")),
        field(t("Name"), name), shared),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), submit] });
    submit.onclick = action(async () => {
      const mine = list.find((p) => p.mine && p.name.toLowerCase() === name.value.trim().toLowerCase());
      if (mine && !(await confirmDialog(t("You already have a template called {name}. Replace its options?", { name: mine.name }),
        { title: t("Replace {name}?", { name: mine.name }), confirmText: t("Replace") }))) return;
      const body = { name: name.value, options: read(), ...(shared ? { shared: shared.input.checked } : {}) };
      store(mine ? await put(`/api/presets/${mine.id}`, body) : await post("/api/presets", body));
      modal.close();
      toast(t("Template {name} saved", { name: name.value.trim() }), "success");
    });
    name.onkeydown = (e) => { if (e.key === "Enter") submit.click(); };
  };

  const node = h("div", { class: "row" }, h("div", { class: "grow" }, choice), update, remove, save);
  // load(): the templates, and the server's defaults for new games (organizers only)
  const load = async () => {
    const data = await get("/api/presets");
    list = data.presets;
    draw();
    return data;
  };
  return { node, load };
}

// What admins must know about the certificate for encrypted game connections (tls.py), or null if all is well
export function tlsBanner(status) {
  if (!status?.enabled) return null;
  const link = h("a", { href: "#/admin/settings" }, t("Admin → Settings"));
  if (!status.ok) {
    return banner("error", icon("lock", 18), h("div", { class: "grow" },
      h("strong", {}, t("Games with encrypted connections cannot start.")), " ", tServer(status.error), " ",
      h("div", { class: "small" }, withNodes(t("Fix the certificate under {where}, or turn off encrypted connections in the games' settings."), { where: link }))));
  }
  if (!status.expires_soon) return null;
  return banner(status.expired ? "error" : "warn", icon("lock", 18), h("div", { class: "grow" },
    h("strong", {}, status.expired ? t("The certificate for encrypted connections expired on {date}.", { date: fmtDate(status.expires_at) })
      : t("The certificate for encrypted connections expires on {date}.", { date: fmtDate(status.expires_at) })), " ",
    h("div", { class: "small" }, t("Players' clients refuse an expired certificate. Renew it: running servers take the new one over by themselves once nobody is connected."))));
}

// The games being played, and those over: finished by everyone, or aborted before that (views/archive.js)
export function gamesTabs(current, archived) {
  return h("div", { class: "tabs", role: "navigation" },
    h("a", { class: "tab", href: "#/games", "aria-current": current === "games" ? "page" : undefined }, t("Current games")),
    h("a", { class: "tab", href: "#/archive", "aria-current": current === "archive" ? "page" : undefined },
      archived ? t("Archive ({n})", { n: archived }) : t("Archive")));
}

export function adminTabs(current) {
  return h("div", { class: "tabs", role: "navigation" },
    [["users", t("Users")], ["settings", t("Settings")], ["log", t("Log")]].map(([key, label]) =>
      h("a", { class: "tab", href: `#/admin/${key}`, "aria-current": key === current ? "page" : undefined }, label)));
}

// ---------------------------------------------------------------------------------------------------------------------
// Archipelago updates (admins only: they come from /api/system)
// ---------------------------------------------------------------------------------------------------------------------
const README_UPDATING = "https://github.com/NiklaskoDev/ArchipelagoManagerDocker#updating";

export function updateBanner(update) {
  if (!update?.update_available) return null;
  return banner("info", icon("package", 18), h("div", { class: "grow" },
    h("strong", {}, t("Archipelago {version} is available", { version: update.latest })), " ",
    t("(this server runs {version}).", { version: update.installed }),
    h("div", { class: "small" }, t("Update the container image to get it: on Unraid, Docker → Check for updates → Apply."), " ",
      t("Users, configs and games are kept, and running games start again.")),
    h("div", { class: "small" },
      update.url ? [h("a", { href: update.url, target: "_blank", rel: "noopener" }, t("Release notes")), " · "] : null,
      h("a", { href: README_UPDATING, target: "_blank", rel: "noopener" }, t("No newer image offered yet?")))));
}

export function updateStatus(update) {
  if (!update.enabled) return h("span", { class: "muted" }, t("Not checked: turned off with UPDATE_CHECK=false"));
  if (update.update_available) {
    return h("span", { class: "row" }, badge(t("{version} available", { version: update.latest }), "warn"),
      update.url ? h("a", { href: update.url, target: "_blank", rel: "noopener" }, t("release notes")) : null);
  }
  if (update.latest) {
    return h("span", { class: "row" }, badge(t("Up to date"), "ok"),
      h("span", { class: "muted small" }, t("latest release {version}, checked {ago}", { version: update.latest, ago: fmtAgo(update.checked_at) })));
  }
  if (update.error) return h("span", { class: "muted", title: tServer(update.error) }, t("Could not reach GitHub to check"));
  return h("span", { class: "muted" }, t("Not checked yet"));
}
