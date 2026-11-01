import { h, clear, icon, toast, action, fmtAgo, fmtDuration, setText } from "../util.js";
import { api, get, isAdmin, isOrganizer, state } from "../api.js";
import { pageHeader, chips, banner, badge, field, checkbox, select, jobBadge, missingGamesPanel, missingRomsPanel, promptMissingGames, empty, apVersionField,
  presetBar, gameSettingsSection } from "../components.js";
import { t, tServer, plural } from "../i18n.js";
import { every } from "../live.js";

const SPOILER_CHOICES = () => [[1, t("Basic spoiler log")], [0, t("No spoiler log")], [2, t("With playthrough")], [3, t("Playthrough and paths")]];
const PLANDO = () => [["bosses", t("Bosses")], ["connections", t("Connections")], ["texts", t("Texts")], ["items", t("Items")]];

export default async function render({ root, cleanup, isCurrent, query }) {
  const admin = isAdmin();
  const presets = presetBar({ read: readOptions, apply: applyOptions });
  const [configs, jobs, apVersions, presetData, status] = await Promise.all([get(`/api/configs${admin ? "?all=true" : ""}`), get("/api/jobs"),
    get("/api/ap-versions"), presets.load(), get("/api/auth/status")]);
  const selected = new Set(query.getAll("config").map(Number));

  // ?from=<job>: generate again with the configs and options of an earlier job
  const fromId = Number(query.get("from")) || null;
  const source = fromId ? await get(`/api/jobs/${fromId}`).catch(() => null) : null;
  if (fromId && !source) toast(t("That job no longer exists."), "error");
  const unavailable = [];
  for (const p of source?.players || []) {
    if (configs.some((c) => c.id === p.config_id)) selected.add(p.config_id);
    else unavailable.push(p);
  }
  const opts = source?.options || {};

  const missingHost = h("div");
  const countLabel = h("span", { class: "muted" });
  const filter = h("input", { class: "input", type: "search", placeholder: t("Filter configs…"), "aria-label": t("Filter configs") });
  const groupsHost = h("div");
  const boxes = new Map();
  const groupSyncs = [];
  const openGroups = new Set();  // the games whose group was opened by hand, kept open while the list is drawn anew

  function updateSelection() {
    setText(countLabel, t("{n} selected", { n: selected.size }));
    groupSyncs.forEach((sync) => sync());
    const chosen = configs.filter((c) => selected.has(c.id));
    const missing = [...new Set(chosen.flatMap((c) => c.missing_games))];
    clear(missingHost).append(missingGamesPanel(missing.map((game) => ({
      game, pending: [], games: [], configs: chosen.filter((c) => c.missing_games.includes(game)).map((c) => ({ id: c.id, name: c.name, owner: c.owner })),
    })), reloadConfigs) || "", missingRomsPanel(chosen.map((c) => c.missing_roms)));
  }
  async function reloadConfigs() {
    const fresh = await get(`/api/configs${admin ? "?all=true" : ""}`);
    if (!isCurrent()) return;
    configs.splice(0, configs.length, ...fresh);
    drawRows();
    updateSelection();
  }
  // The configs by game, each game a group that opens: only the games there are configs for. A group with chosen configs
  // or ones the filter found is open; a config of several games (several players, or weights over games) is in a group
  // of its own at the end.
  const SEVERAL = "";
  const groupOf = (c) => (c.games.length === 1 ? c.games[0] : SEVERAL);
  function drawRows() {
    const q = filter.value.trim().toLowerCase();
    boxes.clear();
    groupSyncs.length = 0;
    const groups = new Map();
    for (const c of configs) {
      const hay = `${c.name} ${c.owner ?? ""} ${c.player_names.join(" ")} ${c.games.join(" ")}`.toLowerCase();
      if (q && !hay.includes(q)) continue;
      if (!groups.has(groupOf(c))) groups.set(groupOf(c), []);
      groups.get(groupOf(c)).push(c);
    }
    const games = [...groups.keys()].sort((a, b) => (a === SEVERAL) - (b === SEVERAL) || a.localeCompare(b));
    clear(groupsHost).append(games.length ? games.map((game) => configGroup(game, groups.get(game), !!q))
      : h("p", { class: "muted list-none" }, t("Nothing matches.")));
  }

  function configGroup(game, list, filtering) {
    const picked = () => list.filter((c) => selected.has(c.id)).length;
    const all = h("input", { type: "checkbox", "aria-label": game === SEVERAL ? t("Choose all configs of several games")
      : t("Choose all {game} configs", { game }) });
    const count = h("span", { class: "muted small" });
    const sync = () => {
      const n = picked();
      all.checked = n === list.length;
      all.indeterminate = n > 0 && n < list.length;
      setText(count, n ? t("{n} of {total} chosen", { n, total: list.length }) : plural(list.length, "{n} config", "{n} configs"));
    };
    all.onchange = () => {
      for (const c of list) {
        if (all.checked) selected.add(c.id); else selected.delete(c.id);
        boxes.get(c.id).checked = all.checked;
      }
      updateSelection();
    };
    const rows = list.map((c) => {
      const box = h("input", { type: "checkbox", checked: selected.has(c.id), "aria-label": t("Include {name}", { name: c.name }) });
      const toggle = () => { if (box.checked) selected.add(c.id); else selected.delete(c.id); updateSelection(); };
      box.addEventListener("change", toggle);
      boxes.set(c.id, box);
      return h("tr", { onclick: (e) => { if (e.target !== box && !e.target.closest("a")) { box.checked = !box.checked; toggle(); } }, style: { cursor: "pointer" } },
        h("td", { class: "select-col" }, box),
        admin ? h("td", {}, c.owner) : null,
        h("td", {}, h("span", { class: "slot-name" }, c.name)),
        h("td", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, t("from file name"))),
        game === SEVERAL ? h("td", {}, chips(c.games, c.missing_games)) : null);
    });
    const missing = game !== SEVERAL && list.some((c) => c.missing_games.includes(game));
    const group = h("details", { class: "config-group", open: filtering || openGroups.has(game) || picked() > 0 },
      h("summary", {}, all, game === SEVERAL ? h("span", { class: "grow" }, t("Several games")) : h("span", { class: "grow" }, chips([game], missing ? [game] : [])),
        count),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", { class: "select-col" }), admin ? h("th", {}, t("Owner")) : null, h("th", {}, t("Config")), h("th", {}, t("Player")),
          game === SEVERAL ? h("th", {}, t("Games")) : null)),
        h("tbody", {}, rows))));
    group.addEventListener("toggle", () => { if (group.open) openGroups.add(game); else openGroups.delete(game); });
    groupSyncs.push(sync);
    sync();
    return group;
  }
  filter.oninput = drawRows;

  // options -------------------------------------------------------------------------------------------------------------
  const name = h("input", { class: "input", placeholder: t("Optional, e.g. Friday run"), maxLength: 120, value: source?.name || "" });
  const spoiler = select(SPOILER_CHOICES(), opts.spoiler ?? 1);
  const seed = h("input", { class: "input", inputMode: "numeric", placeholder: t("Random"), pattern: "[0-9]*" });
  const race = checkbox(t("Race mode"), !!opts.race, t("Spoiler log is withheld and patches are encrypted where supported."));
  const skipBalance = checkbox(t("Skip progression balancing"), !!opts.skip_prog_balancing);
  const plando = PLANDO().map(([key, label]) => ({ key, box: checkbox(label, (opts.plando || ["bosses", "connections", "texts"]).includes(key)) }));
  const meta = h("textarea", { class: "input", rows: 8, placeholder: t("meta_description: My meta file\nnull:\n  progression_balancing: 50"), spellcheck: false, value: source?.meta || "" });
  const version = apVersionField(apVersions, opts.ap_version || "");
  const metaDetails = h("details", { open: !!source?.meta }, h("summary", { class: "muted" }, t("Advanced: meta.yaml")),
    h("div", { class: "stack", style: { marginTop: "10px" } },
      h("p", { class: "muted small" }, t("A meta file can override player options for everyone. It must contain a meta_description.")), meta));
  const hostAfter = checkbox(t("Start the server when generation finishes"), source && !source.test ? source.host_after : true);
  // organizers and administrators set up the game it makes; a player who generates does not run it
  const gameSettings = isOrganizer() ? gameSettingsSection(opts.server_options, presetData.server_defaults) : null;

  // what a template (presets.py) holds: everything but the name, the seed and the players
  function readOptions() {
    return { spoiler: Number(spoiler.value), race: race.input.checked, skip_prog_balancing: skipBalance.input.checked,
      plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), host_after: hostAfter.input.checked,
      meta: meta.value.trim() || null, ap_version: version ? version.read() || null : null,
      server_options: gameSettings ? gameSettings.read() : null };
  }
  function applyOptions(o) {
    spoiler.value = String(o.spoiler);
    race.input.checked = o.race;
    skipBalance.input.checked = o.skip_prog_balancing;
    for (const p of plando) p.box.input.checked = o.plando.includes(p.key);
    hostAfter.input.checked = o.host_after;
    meta.value = o.meta || "";
    if (o.meta) metaDetails.open = true;
    const left = [];
    if (version ? !version.set(o.ap_version) : o.ap_version) left.push(t("Archipelago {version} is not installed here, so this server's version is used.", { version: o.ap_version }));
    if (gameSettings) gameSettings.apply(o.server_options);
    else if (o.server_options) left.push(t("Its settings for the new game apply only when an organizer generates."));
    return left.join(" ");
  }
  // what a player (not an organizer) may have of the games they generate (limits.py)
  const limits = status.limits;
  const limitsNote = !limits ? null : limits.unfinished_now >= limits.unfinished
    ? banner("warn", icon("alert", 16), h("div", { class: "grow" },
      t("You have {games} unfinished games of your own, the most you can have at a time. Move one to the archive to generate another.", { games: limits.unfinished }),
      " ", h("a", { href: "#/games" }, t("Your games"))))
    : h("p", { class: "muted small", style: { margin: 0 } }, t("A game you generate can have up to {worlds} worlds. You can have {games} unfinished games of your own at a time, one of them running: {left} more for now.",
      { worlds: limits.worlds, games: limits.unfinished, left: limits.unfinished - limits.unfinished_now }));
  const errorHost = h("div");
  const submit = h("button", { class: "btn primary", type: "button" }, icon("zap", 14), t("Generate"));
  submit.onclick = action(async () => {
    clear(errorHost);
    if (!selected.size) throw new Error(t("Select at least one config"));
    if (seed.value && !/^\d+$/.test(seed.value.trim())) throw new Error(t("The seed must be a number"));
    try {
      const job = await api("POST", "/api/jobs", {
        config_ids: [...selected], name: name.value || null, spoiler: Number(spoiler.value), race: race.input.checked,
        seed: seed.value.trim() ? Number(seed.value.trim()) : null, skip_prog_balancing: skipBalance.input.checked,
        plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), meta: meta.value.trim() || null,
        host_after: hostAfter.input.checked, ap_version: version ? version.read() || null : null,
        ...(gameSettings ? { server_options: gameSettings.read() } : {}),
      });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      errorHost.append(banner("error", icon("alert", 16), err.message));
      if (err.data?.missing_games?.length) {
        const chosen = configs.filter((c) => selected.has(c.id));
        promptMissingGames(err.data.missing_games.map((game) => ({
          game, configs: chosen.filter((c) => c.missing_games.includes(game)).map((c) => ({ id: c.id, name: c.name, owner: c.owner })),
        })), reloadConfigs);
      }
      throw new Error(t("Generation was not started"));
    }
  });

  const jobsHost = h("div");
  function drawJobs(list) {
    clear(jobsHost);
    if (!list.length) { jobsHost.append(h("p", { class: "muted" }, t("Nothing generated yet."))); return; }
    jobsHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("Job"), t("Status"), t("Started"), t("Result"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, list.slice(0, 12).map((j) => h("tr", {},
        h("td", {}, h("a", { href: `#/jobs/${j.id}`, class: "slot-name" }, j.name || t("Job #{id}", { id: j.id })), j.test ? [" ", badge(t("Test"), "accent")] : null,
          admin && j.creator ? h("div", { class: "muted small" }, j.creator) : null),
        h("td", {}, jobBadge(j.status), j.queue_position ? h("span", { class: "muted small" }, " ", t("#{n} in queue", { n: j.queue_position })) : null),
        h("td", { class: "muted" }, fmtAgo(j.started_at || j.created_at)),
        h("td", {}, j.game_id ? h("a", { href: `#/games/${j.game_id}` }, t("Open game"))
          : j.test && j.status === "succeeded" ? h("span", { class: "muted small" }, t("Test passed"))
          : j.error ? h("span", { class: "muted small" }, tServer(j.error.split("\n")[0]).slice(0, 70)) : "—"),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          !j.test && !["queued", "running"].includes(j.status) ? h("a", { class: "btn sm", href: `#/generate?from=${j.id}`, title: t("Generate again"), "aria-label": t("Generate again") }, icon("restart", 14)) : null,
          h("a", { class: "btn sm", href: `#/jobs/${j.id}` }, t("Log"))))))))));
  }
  async function pollJobs() {
    const list = await get("/api/jobs");
    if (isCurrent()) drawJobs(list);
  }

  if (!configs.length) {
    root.append(pageHeader(t("Generate"), t("Create a new multiworld from player configs.")),
      empty(t("No configs to generate from"), admin ? t("Players can upload their YAML files under “My configs”.") : t("Upload a config first."),
        h("a", { class: "btn primary", href: "#/configs" }, t("Go to configs"))));
    root.append(h("div", { class: "section" }, h("h2", {}, t("Recent jobs")), jobsHost));
    drawJobs(jobs);
  } else {
    root.append(
      pageHeader(t("Generate"), admin ? t("Pick the players for a new multiworld, then generate it.") : t("Generate a multiworld from your own configs.")),
      source ? banner("info", icon("restart", 18), h("div", { class: "grow" },
        h("strong", {}, t("Generating again:")), " ", h("a", { href: `#/jobs/${source.id}` }, source.name || t("Job #{id}", { id: source.id })), ". ",
        t("Its configs and options are filled in. The configs are used as they are now, with a new seed."),
        unavailable.length ? h("div", { class: "small" }, t("No longer available, so not selected: {players}",
          { players: unavailable.map((p) => (p.names.join(", ") || p.filename) + (admin && p.owner ? ` (${p.owner})` : "")).join(", ") })) : null)) : null,
      limitsNote,
      missingHost,
      h("div", { class: "card flush" },
        h("div", { class: "row spread", style: { padding: "14px 16px" } }, h("div", { class: "grow" }, filter), countLabel),
        groupsHost),
      h("div", { class: "card stack section" },
        h("h2", {}, t("Options")),
        presets.node,
        h("div", { class: "form-grid" }, field(t("Name"), name, t("Shown in the games list")), field(t("Spoiler log"), spoiler), field(t("Seed"), seed, t("Leave empty for a random seed")),
          version?.node),
        h("div", { class: "stack tight" }, race, skipBalance, hostAfter),
        h("div", { class: "stack tight" }, h("span", { class: "field-label" }, t("Allowed plando")), h("div", { class: "row" }, plando.map((p) => p.box))),
        metaDetails, gameSettings?.node,
        errorHost, h("div", { class: "row" }, submit)),
      h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Recent jobs"))), jobsHost));
    drawRows();
    updateSelection();
    drawJobs(jobs);
  }
  every(3000, pollJobs, cleanup);
}
