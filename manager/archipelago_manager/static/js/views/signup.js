import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo, fmtDate, fmtUntil, toLocalInput, fromLocalInput } from "../util.js";
import { api, get, post, patch, del, state } from "../api.js";
import { banner, badge, chips, field, checkbox, select, missingGamesPanel, missingRomsPanel, promptMissingGames, presetBar, gameSettingsSection } from "../components.js";
import { t, plural, tServer } from "../i18n.js";

const SPOILER_CHOICES = [[1, t("Basic spoiler log")], [0, t("No spoiler log")], [2, t("With playthrough")], [3, t("Playthrough and paths")]];
const PLANDO = [["bosses", t("Bosses")], ["connections", t("Connections")], ["texts", t("Texts")], ["items", t("Items")]];

export function signupBadge(signup) {
  if (signup.status === "open") return signup.open ? badge(t("Open for entries"), "ok") : badge(t("Closing…"), "warn");
  return { closed: badge(t("Closed"), "muted"), generating: badge(t("Generating…"), "warn"), done: badge(t("Game ready"), "ok") }[signup.status];
}

export function deadlineText(signup) {
  if (signup.status !== "open") return signup.closed_at ? t("Closed {date}", { date: fmtDate(signup.closed_at) }) : null;
  return signup.deadline ? t("Closes {date} ({until})", { date: fmtDate(signup.deadline), until: fmtUntil(signup.deadline) })
    : t("No deadline: the organizer closes it");
}

// Create (no `signup`) or edit a round. `onSaved` receives the round as the server returns it.
export async function openSignupForm({ signup = null, onSaved }) {
  const presets = presetBar({ read: readOptions, apply: applyOptions });
  const [worlds, presetData] = await Promise.all([get("/api/worlds").catch(() => ({ games: [] })),
    presets.load().catch(() => ({ server_defaults: null }))]);
  const known = worlds.games.map((g) => g.name);
  const opts = signup?.options || { spoiler: 1, race: false, skip_prog_balancing: false, plando: ["bosses", "connections", "texts"], host_after: true,
    server_options: null };

  const name = h("input", { class: "input", maxLength: 120, value: signup?.name || "", placeholder: t("e.g. Friday run") });
  const description = h("textarea", { class: "input", rows: 3, maxLength: 2000, value: signup?.description || "",
    placeholder: t("Optional: what this round is about, when you play, house rules…") });
  const deadline = h("input", { class: "input", type: "datetime-local", value: toLocalInput(signup?.deadline) });
  const noDeadline = checkbox(t("No deadline: close the sign-ups by hand"), signup ? !signup.deadline : false);
  const syncDeadline = () => { deadline.disabled = noDeadline.input.checked; };
  noDeadline.input.addEventListener("change", syncDeadline);
  syncDeadline();
  if (!signup) {  // suggest a week from now at 20:00
    const d = new Date(Date.now() + 7 * 86400000);
    d.setHours(20, 0, 0, 0);
    deadline.value = toLocalInput(d.getTime() / 1000);
  }
  const perUser = h("input", { class: "input", type: "number", min: 1, max: 20, value: signup?.configs_per_user ?? 1 });

  // allowed games: a list of chips, filled from an input that suggests the installed games
  const allowed = [...(signup?.allowed_games || [])];
  const chipsHost = h("div", { class: "chips" });
  const listId = `games-${Math.random().toString(36).slice(2)}`;
  const gameInput = h("input", { class: "input", list: listId, placeholder: t("Type a game and press Enter") });
  const drawAllowed = () => {
    clear(chipsHost).append(allowed.length ? allowed.map((g) => h("span", { class: "chip" }, g,
      h("button", { class: "btn ghost icon-only sm", type: "button", "aria-label": t("Remove {game}", { game: g }),
        onclick: () => { allowed.splice(allowed.indexOf(g), 1); drawAllowed(); } }, icon("x", 12))))
      : h("span", { class: "muted small" }, t("Any game installed on this server")));
  };
  const addGame = () => {
    const value = gameInput.value.trim();
    if (!value) return;
    const match = known.find((g) => g.toLowerCase() === value.toLowerCase());
    if (!match) { toast(t("\"{game}\" is not installed on this server", { game: value }), "error"); return; }
    if (!allowed.includes(match)) allowed.push(match);
    gameInput.value = "";
    drawAllowed();
  };
  gameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addGame(); } });
  drawAllowed();

  // a mystery round: players join with a name, and everyone's game and options are rolled from one file of weights
  const mysteryBox = checkbox(t("Mystery round: everyone plays what one file of weights rolls for them"), !!signup?.mystery,
    signup?.entry_count ? t("A round becomes a mystery round, or stops being one, only while nobody has entered.")
      : t("Players join with just a player name. Nobody knows their game before it is generated."));
  mysteryBox.input.disabled = !!signup?.entry_count;
  const weightsText = signup?.mystery ? await fetch(`/api/signups/${signup.id}/mystery.yaml`, { credentials: "same-origin" })
    .then((r) => (r.ok ? r.text() : "")) : "";
  const weights = h("textarea", { class: "input editor", rows: 10, spellcheck: false, wrap: "off", value: weightsText,
    placeholder: "name: Player\ngame:\n  A Link to the Past: 1\n  Super Metroid: 1\nA Link to the Past: {}\nSuper Metroid: {}" });
  const weightsFile = h("input", { type: "file", accept: ".yaml,.yml", hidden: true });
  weightsFile.onchange = async () => {
    if (weightsFile.files[0]) weights.value = await weightsFile.files[0].text();
    weightsFile.value = "";
  };
  const weightsField = field(t("Weights"), h("div", { class: "stack tight" }, weights,
    h("div", { class: "row" }, h("button", { class: "btn sm", type: "button", onclick: () => weightsFile.click() }, icon("upload", 14), t("Load a file")), weightsFile)),
    t("One player file with weights over games and options, like a mystery yaml: written from the games above, or by hand. Every entry is this file with its player's name; changing it changes them all."));
  // or put the weights together from a list of games: how often each comes up, and its default or random options
  const picks = [];
  const picksHost = h("div", { class: "stack tight" });
  const pickInput = h("input", { class: "input", list: listId, placeholder: t("Type a game and press Enter") });
  const drawPicks = () => {
    clear(picksHost).append(picks.length ? picks.map((pick) => {
      const weight = h("input", { class: "input opt-number sm", type: "number", min: 0, max: 1000, value: pick.weight,
        "aria-label": t("Weight of {game}", { game: pick.game }) });
      weight.oninput = () => { pick.weight = Math.max(0, Math.round(Number(weight.value) || 0)); };
      const how = select([["default", t("Its default options")], ["random", t("Every option at random")]], pick.random ? "random" : "default");
      how.setAttribute("aria-label", t("Options of {game}", { game: pick.game }));
      how.onchange = () => { pick.random = how.value === "random"; };
      return h("div", { class: "row", style: { flexWrap: "nowrap" } },
        h("span", { class: "grow" }, pick.game), weight, h("div", { style: { width: "220px" } }, how),
        h("button", { class: "btn ghost icon-only sm", type: "button", "aria-label": t("Remove {game}", { game: pick.game }),
          onclick: () => { picks.splice(picks.indexOf(pick), 1); drawPicks(); } }, icon("x", 12)));
    }) : h("span", { class: "muted small" }, t("No games picked yet.")));
  };
  const addPick = () => {
    const value = pickInput.value.trim();
    if (!value) return;
    const match = known.find((g) => g.toLowerCase() === value.toLowerCase());
    if (!match) { toast(t("\"{game}\" is not installed on this server", { game: value }), "error"); return; }
    if (!picks.some((p) => p.game === match)) picks.push({ game: match, weight: 1, random: false });
    pickInput.value = "";
    drawPicks();
  };
  pickInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addPick(); } });
  drawPicks();
  const writeWeights = h("button", { class: "btn sm", type: "button" }, icon("edit", 14), t("Write the weights"));
  writeWeights.onclick = action(async () => {
    if (!picks.length) throw new Error(t("Pick at least one game"));
    if (weights.value.trim() && !(await confirmDialog(t("The weights below are replaced by ones for the picked games."),
      { title: t("Replace the weights?"), confirmText: t("Replace") }))) return;
    try {
      weights.value = (await post("/api/mystery-weights", { games: picks })).yaml;
    } catch (err) {
      if (err.data?.missing_games?.length) promptMissingGames(err.data.missing_games.map((game) => ({ game })));
      throw err;
    }
  });
  const builder = h("div", { class: "card stack tight", style: { background: "var(--surface-2)" } },
    h("span", { class: "field-label" }, t("Pick the games")),
    h("p", { class: "muted small", style: { margin: 0 } }, t("Each game comes up as often as its weight says, against the others: 3 and 1 means three times as often. With every option at random, each option that can be left to chance is, except deathlink, progression balancing and accessibility.")),
    picksHost,
    h("div", { class: "row" }, h("div", { class: "grow" }, pickInput),
      h("button", { class: "btn sm", type: "button", onclick: addPick }, icon("plus", 14), t("Add")), writeWeights));

  const allowedField = field(t("Allowed games"), h("div", { class: "stack tight" }, chipsHost, h("div", { class: "row" }, h("div", { class: "grow" }, gameInput),
    h("button", { class: "btn sm", type: "button", onclick: addGame }, icon("plus", 14), t("Add")))),
    t("Leave empty to allow every installed game."));
  const syncMystery = () => {
    weightsField.hidden = builder.hidden = !mysteryBox.input.checked;
    allowedField.hidden = mysteryBox.input.checked;
  };
  mysteryBox.input.addEventListener("change", syncMystery);

  const autoGenerate = checkbox(t("Generate the game automatically when sign-ups close"), signup ? signup.auto_generate : true);
  const hostAfter = checkbox(t("Start the server once the game is generated"), opts.host_after);
  const spoiler = select(SPOILER_CHOICES, opts.spoiler);
  const race = checkbox(t("Race mode"), opts.race, t("Spoiler log is withheld and patches are encrypted where supported."));
  const skipBalance = checkbox(t("Skip progression balancing"), opts.skip_prog_balancing);
  const plando = PLANDO.map(([key, label]) => ({ key, box: checkbox(label, opts.plando.includes(key)) }));
  const gameSettings = presetData.server_defaults ? gameSettingsSection(opts.server_options, presetData.server_defaults) : null;
  const generationDetails = h("details", {}, h("summary", { class: "muted" }, t("Generation options")),
    h("div", { class: "stack", style: { marginTop: "10px" } },
      field(t("Spoiler log"), spoiler), h("div", { class: "stack tight" }, race, skipBalance),
      h("div", { class: "stack tight" }, h("span", { class: "field-label" }, t("Allowed plando")), h("div", { class: "row" }, plando.map((p) => p.box)))));

  // what a template (presets.py) holds, as far as a round has it: no meta.yaml and no Archipelago version of its own
  function readOptions() {
    return { spoiler: Number(spoiler.value), race: race.input.checked, skip_prog_balancing: skipBalance.input.checked,
      plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), host_after: hostAfter.input.checked,
      meta: null, ap_version: null, server_options: gameSettings ? gameSettings.read() : null };
  }
  function applyOptions(o) {
    spoiler.value = String(o.spoiler);
    race.input.checked = o.race;
    skipBalance.input.checked = o.skip_prog_balancing;
    for (const p of plando) p.box.input.checked = o.plando.includes(p.key);
    hostAfter.input.checked = o.host_after;
    generationDetails.open = true;
    gameSettings?.apply(o.server_options);
    return o.meta || o.ap_version ? t("A round has no meta.yaml or Archipelago version of its own, so the template's are left out.") : "";
  }

  const save = h("button", { class: "btn primary", type: "button" }, signup ? t("Save") : t("Open sign-ups"));
  save.onclick = action(async () => {
    let when = null;
    if (!noDeadline.input.checked) {
      when = fromLocalInput(deadline.value);
      if (!when) throw new Error(t("Pick a deadline, or choose “No deadline”"));
    }
    const body = {
      name: name.value, description: description.value, deadline: when, configs_per_user: Number(perUser.value),
      allowed_games: allowed, auto_generate: autoGenerate.input.checked, mystery: mysteryBox.input.checked,
      ...(mysteryBox.input.checked && weights.value !== weightsText ? { mystery_weights: weights.value } : {}),
      options: { spoiler: Number(spoiler.value), race: race.input.checked, skip_prog_balancing: skipBalance.input.checked,
        plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), host_after: hostAfter.input.checked,
        server_options: gameSettings ? gameSettings.read() : opts.server_options },
    };
    let result;
    try {
      result = signup ? await patch(`/api/signups/${signup.id}`, body) : await post("/api/signups", body);
    } catch (err) {
      if (err.data?.missing_games?.length) promptMissingGames(err.data.missing_games.map((game) => ({ game })));
      throw err;
    }
    modal.close();
    toast(signup ? t("Round saved") : t("Sign-ups are open"), "success");
    onSaved?.(result);
  });

  const modal = openModal({
    title: signup ? t("Edit round") : t("New sign-up round"), wide: true,
    body: h("div", { class: "stack" },
      field(t("Name"), name, t("Also the name of the generated game")),
      field(t("Description"), description),
      h("div", { class: "form-grid" },
        field(t("Deadline"), deadline, signup?.status === "closed" ? t("Reopen the round to take entries again.") : t("Sign-ups close automatically at this time.")),
        field(t("Configs per player"), perUser)),
      noDeadline,
      mysteryBox, builder, weightsField, allowedField,
      h("datalist", { id: listId }, known.map((g) => h("option", { value: g }))),
      h("div", { class: "stack tight" }, autoGenerate, hostAfter),
      presets.node, generationDetails, gameSettings?.node),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save],
  });
  syncMystery();
}

export default async function render({ root, params, cleanup, isCurrent }) {
  const id = Number(params[0]);
  let signup = await get(`/api/signups/${id}`);
  const manager = signup.can_manage;  // its organizer, or an administrator
  let configs = await get("/api/configs");
  const host = h("div");
  let signature = "";

  async function refresh(force = false) {
    const fresh = await get(`/api/signups/${id}`);
    if (!isCurrent()) return;
    const sig = JSON.stringify(fresh);
    if (!force && sig === signature) return;
    signup = fresh;
    signature = sig;
    draw();
  }
  async function reloadConfigs() {
    configs = await get("/api/configs");
    if (isCurrent()) await refresh(true);
  }
  // Every change to a round answers with the round; errors with missing games offer the upload prompt.
  const apply = (result) => {
    signup = result;
    signature = JSON.stringify(result);
    for (const warning of result.warnings || []) toast(tServer(warning), "info", 9000);
    draw();
  };
  const run = async (method, path, body) => {
    try {
      apply(await api(method, path, body));
    } catch (err) {
      if (err.data?.missing_games?.length) promptMissingGames(err.data.missing_games.map((game) => ({ game })), () => refresh(true));
      throw err;
    }
  };

  // ---------------------------------------------------------------------------------------------------------------------
  function header() {
    const actions = [];
    if (manager) {
      actions.push(h("button", { class: "btn", type: "button", disabled: !["open", "closed"].includes(signup.status),
        onclick: action(() => openSignupForm({ signup, onSaved: apply })) }, icon("edit", 14), t("Edit")));
      if (signup.status === "open") {
        actions.push(h("button", { class: "btn", type: "button", onclick: action(async () => {
          const text = signup.auto_generate && signup.entry_count ? t("Nobody can enter after this, and the game is generated right away.") : t("Nobody can enter after this.");
          if (await confirmDialog(text, { title: t("Close sign-ups now?"), confirmText: t("Close sign-ups") })) await run("POST", `/api/signups/${id}/close`);
        }) }, icon("stop", 14), t("Close now")));
      }
      if (["open", "closed"].includes(signup.status)) {
        actions.push(h("button", { class: "btn", type: "button", disabled: !signup.entry_count || testRunning(),
          title: t("Generate the entries once without keeping the result, to find problems before the real generation"),
          onclick: action(() => run("POST", `/api/signups/${id}/test`)) }, icon("check", 14), t("Test generation")));
      }
      if (signup.status === "closed") {
        actions.push(h("button", { class: "btn", type: "button", onclick: action(reopenDialog) }, icon("restart", 14), t("Reopen")));
        actions.push(h("button", { class: "btn primary", type: "button", disabled: !signup.entry_count,
          onclick: action(() => run("POST", `/api/signups/${id}/generate`)) }, icon("zap", 14), t("Generate")));
      }
      actions.push(h("button", { class: "btn danger", type: "button", disabled: signup.status === "generating", onclick: action(async () => {
        if (!(await confirmDialog(t("The entries are removed. Configs in the players' libraries and a generated game stay."),
          { title: t("Delete this round?"), confirmText: t("Delete"), danger: true }))) return;
        await del(`/api/signups/${id}`);
        toast(t("Round deleted"), "success");
        location.hash = "#/signups";
      }) }, icon("trash", 14), t("Delete")));
    }
    return h("div", {},
      h("div", { class: "muted small" }, h("a", { href: "#/signups" }, t("← All sign-ups"))),
      h("div", { class: "page-head", style: { marginTop: "8px" } },
        h("div", {}, h("div", { class: "row" }, h("h1", {}, signup.name), signupBadge(signup)),
          h("p", { class: "muted" }, [deadlineText(signup), signup.creator ? t("by {name}", { name: signup.creator }) : null].filter(Boolean).join(" · "))),
        h("div", { class: "page-actions" }, actions)));
  }

  function reopenDialog() {
    const deadline = h("input", { class: "input", type: "datetime-local" });
    const d = new Date(Date.now() + 2 * 86400000);
    d.setHours(20, 0, 0, 0);
    deadline.value = toLocalInput(d.getTime() / 1000);
    const noDeadline = checkbox(t("No deadline"), false);
    noDeadline.input.addEventListener("change", () => { deadline.disabled = noDeadline.input.checked; });
    const go = h("button", { class: "btn primary", type: "button" }, t("Reopen"));
    go.onclick = action(async () => {
      const when = noDeadline.input.checked ? null : fromLocalInput(deadline.value);
      if (!noDeadline.input.checked && !when) throw new Error(t("Pick a deadline"));
      await run("POST", `/api/signups/${id}/reopen`, { deadline: when });
      modal.close();
    });
    const modal = openModal({ title: t("Reopen sign-ups"), body: h("div", { class: "stack" }, field(t("New deadline"), deadline), noDeadline),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), go] });
  }

  function stateBanner() {
    if (signup.status === "open" && signup.open) {
      return banner("info", icon("calendar", 18), h("div", { class: "grow" },
        h("strong", {}, t("Sign-ups are open.")), " ",
        signup.mystery ? (signup.deadline ? t("Join with a player name {until}, until {date}.", { until: fmtUntil(signup.deadline), date: fmtDate(signup.deadline) })
          : t("Join with a player name until the organizer closes the round."))
          : signup.deadline ? t("Enter your config {until}, until {date}.", { until: fmtUntil(signup.deadline), date: fmtDate(signup.deadline) })
            : t("Enter your config until the organizer closes the round."),
        h("div", { class: "small muted" }, t("You can still change or withdraw your entry until then."))));
    }
    if (signup.status === "generating") {
      return banner("info", h("span", { class: "spinner" }), h("div", { class: "grow" }, h("strong", {}, t("Generating the game…")),
        manager && signup.job_id ? [" ", h("a", { href: `#/jobs/${signup.job_id}` }, t("Show the log"))] : null));
    }
    if (signup.status === "done") {
      return banner("ok", icon("check", 18), h("div", { class: "grow" }, h("strong", {}, t("The game is ready.")), " ",
        t("Everyone who entered was added to it.")), signup.game_id ? h("a", { class: "btn sm primary", href: `#/games/${signup.game_id}` }, t("Open the game")) : null);
    }
    if (signup.status === "closed" || !signup.open) {
      return [
        banner("warn", icon("alert", 18), h("div", { class: "grow" }, h("strong", {}, t("Sign-ups are closed.")), " ",
          manager ? t("Generate the game when you are ready, or reopen the round.") : t("The game will be generated soon."))),
        manager && signup.error ? banner("error", icon("alert", 18), h("div", { class: "grow" }, h("strong", {}, t("Last attempt failed:")), " ", tServer(signup.error))) : null,
      ];
    }
    return null;
  }

  const testRunning = () => ["queued", "running"].includes(signup.test?.status);

  // The latest test generation, for whoever will generate the round
  function testBanner() {
    const test = signup.test;
    if (!test || !["open", "closed"].includes(signup.status)) return null;
    const log = h("a", { href: `#/jobs/${test.job_id}` }, t("Show the log"));
    const changed = test.current ? null : h("div", { class: "small" }, t("Entries or options have changed since."));
    if (testRunning()) {
      return banner("info", h("span", { class: "spinner" }), h("div", { class: "grow" }, h("strong", {}, t("Testing the entries…")), " ",
        test.queue_position ? t("#{n} in queue", { n: test.queue_position }) + " · " : "", log));
    }
    if (test.status === "succeeded") {
      return banner(test.current ? "ok" : "info", icon("check", 18), h("div", { class: "grow" },
        h("strong", {}, t("Test passed {ago}.", { ago: fmtAgo(test.finished_at) })), " ",
        t("The entries generate together. A different seed can still fail in rare cases."), " ", log, changed));
    }
    if (test.status === "failed") {
      return banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, t("Test failed {ago}.", { ago: fmtAgo(test.finished_at) })), " ", log,
        h("pre", { class: "mono", style: { margin: "6px 0 0", whiteSpace: "pre-wrap" } }, tServer(test.error) || ""),
        signup.entries.some((e) => e.generation_error) ? h("div", { class: "small" }, t("The entries the generator complained about are marked below.")) : null,
        changed));
    }
    return null;
  }

  function generationError(e, cls = "") {
    return e.generation_error ? h("div", { class: `form-error ${cls}` }, t("The generator could not use this config:"),
      h("pre", { class: "mono", style: { margin: "4px 0 0", whiteSpace: "pre-wrap" } }, e.generation_error)) : null;
  }

  function rulesCard() {
    const o = signup.options;
    const extras = [o.race ? t("race mode") : null, t("spoiler level {level}", { level: o.spoiler }), o.host_after ? t("server starts after generating") : null,
      signup.auto_generate ? t("generated as soon as sign-ups close") : t("generated by an administrator")].filter(Boolean);
    return h("div", { class: "card stack" },
      signup.description ? h("p", { style: { whiteSpace: "pre-wrap", margin: 0 } }, signup.description) : null,
      h("dl", { class: "kv" },
        signup.mystery ? [h("dt", {}, t("Mystery")), h("dd", { class: "stack tight" },
          h("span", {}, t("Everyone joins with a player name and plays what the round's weights roll for them, out of these games:")),
          chips(signup.mystery_games), h("a", { href: `/api/signups/${id}/mystery.yaml` }, icon("download", 14), " ", t("The weights")))]
          : [h("dt", {}, t("Games")), h("dd", {}, signup.allowed_games.length ? chips(signup.allowed_games) : h("span", { class: "muted" }, t("Any game installed on this server")))],
        h("dt", {}, t("Configs per player")), h("dd", {}, String(signup.configs_per_user)),
        h("dt", {}, t("Entries")), h("dd", {}, plural(signup.user_count, "{entries} from {n} player", "{entries} from {n} players", { entries: signup.entry_count })),
        h("dt", {}, t("Settings")), h("dd", { class: "muted" }, extras.join(" · "))));
  }

  // ---------------------------------------------------------------------------------------------------------------------
  function myCard() {
    const mine = signup.entries.filter((e) => e.mine);
    // Admins can still fix entries after the deadline, until the game is generated
    const editable = signup.open || (manager && signup.status === "closed");
    const canEnter = editable && mine.length < signup.configs_per_user;
    if (!mine.length && !editable) return null;
    const entered = new Set(mine.map((e) => e.config_id));
    const choices = configs.filter((c) => !entered.has(c.id));
    const missing = [...new Set(mine.flatMap((e) => e.missing_games))];

    if (signup.mystery) return mysteryCard(mine, editable, canEnter);
    const picker = choices.length ? select(choices.map((c) => [c.id, `${c.name} (${c.player_names.join(", ") || t("name from file")} · ${c.games.join(", ")})`]), choices[0].id) : null;
    const enter = picker ? h("button", { class: "btn primary", type: "button",
      onclick: action(() => run("POST", `/api/signups/${id}/entries`, { config_id: Number(picker.value) })) }, icon("check", 14), t("Enter")) : null;
    const file = h("input", { type: "file", accept: ".yaml,.yml", hidden: true });
    file.onchange = async () => {
      const chosen = file.files[0];
      file.value = "";
      if (!chosen) return;
      try {
        const form = new FormData();
        form.append("files", chosen);
        const uploaded = await api("POST", "/api/configs", form);
        configs = await get("/api/configs");
        await run("POST", `/api/signups/${id}/entries`, { config_id: uploaded.created[0].id });
        toast(t("Uploaded to your configs and entered"), "success");
      } catch (err) { toast(err.message, "error"); draw(); }
    };

    return h("div", { class: "card stack" },
      h("h2", {}, mine.length > 1 ? t("Your entries") : t("Your entry")),
      mine.length ? h("div", { class: "stack" }, mine.map((e) => h("div", { class: "stack tight" },
        h("div", { class: "row spread" },
          h("div", {}, h("span", { class: "slot-name" }, e.player_names.join(", ") || e.name), " ", chips(e.games, e.missing_games),
            h("div", { class: "muted small" }, `${e.name} · ${t("entered {ago}", { ago: fmtAgo(e.created_at) })}`,
              e.updated_at > e.created_at + 1 ? `, ${t("updated {ago}", { ago: fmtAgo(e.updated_at) })}` : "")),
          h("div", { class: "row" },
            h("a", { class: "btn sm", href: `/api/signups/${id}/entries/${e.id}/download` }, icon("download", 14), "YAML"),
            signup.open ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              if (await confirmDialog(t("Your config stays in your library."), { title: t("Withdraw this entry?"), confirmText: t("Withdraw") })) await run("DELETE", `/api/signups/${id}/entries/${e.id}`);
            }) }, t("Withdraw")) : null)),
        e.outdated && editable ? banner("info", icon("edit", 16), h("div", { class: "grow" }, t("You changed this config after entering it. The round still has the old version.")),
          h("button", { class: "btn sm primary", type: "button", onclick: action(() => run("POST", `/api/signups/${id}/entries/${e.id}/refresh`)) }, t("Use the new version"))) : null,
        e.issues.map((issue) => h("div", { class: "form-error" }, tServer(issue))), generationError(e)))) : h("p", { class: "muted" }, t("You have not entered yet.")),
      // the round's organizer sees these for every entry at the top of the page
      !manager && missing.length ? missingGamesPanel(missing.map((game) => ({ game, configs: [], games: [], pending: [] })), reloadConfigs) : null,
      manager ? null : missingRomsPanel(mine.map((e) => e.missing_roms)),
      canEnter ? h("div", { class: "stack tight" },
        h("span", { class: "field-label" }, mine.length ? t("Enter another config") : t("Enter with one of your configs")),
        h("div", { class: "row" }, picker ? h("div", { class: "grow" }, picker) : h("span", { class: "muted" }, configs.length ? t("You have no other configs.") : t("You have no configs yet.")), enter,
          h("button", { class: "btn", type: "button", onclick: () => file.click() }, icon("upload", 14), t("Upload a YAML")),
          h("a", { class: "btn ghost", href: "#/configs/new" }, t("Create a config"))), file) : null);
  }

  // A mystery round takes a player name, nothing more: the entry is the round's weights with that name
  function mysteryCard(mine, editable, canEnter) {
    const playerName = h("input", { class: "input", maxLength: 16, value: mine.length ? "" : state.user.username.slice(0, 16),
      placeholder: t("Your player name"), "aria-label": t("Player name") });
    const join = h("button", { class: "btn primary", type: "button" }, icon("dice", 14), t("Join"));
    join.onclick = action(async () => {
      if (!playerName.value.trim()) throw new Error(t("Enter a player name"));
      await run("POST", `/api/signups/${id}/mystery-entries`, { player_name: playerName.value.trim() });
      toast(t("You are in. Your game is rolled when the game is generated."), "success");
    });
    return h("div", { class: "card stack" },
      h("h2", {}, mine.length > 1 ? t("Your entries") : t("Your entry")),
      mine.length ? h("div", { class: "stack" }, mine.map((e) => h("div", { class: "row spread" },
        h("div", {}, h("span", { class: "slot-name" }, e.player_names.join(", ")), " ", badge(t("Mystery"), "accent"),
          h("div", { class: "muted small" }, t("entered {ago}", { ago: fmtAgo(e.created_at) }))),
        h("div", { class: "row" },
          signup.open ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
            if (await confirmDialog(t("You can join again as long as the round is open."), { title: t("Withdraw this entry?"), confirmText: t("Withdraw") })) await run("DELETE", `/api/signups/${id}/entries/${e.id}`);
          }) }, t("Withdraw")) : null),
        e.issues.map((issue) => h("div", { class: "form-error" }, tServer(issue)))))) : h("p", { class: "muted" }, t("You have not joined yet.")),
      canEnter ? h("div", { class: "stack tight" },
        h("span", { class: "field-label" }, mine.length ? t("Join with another player name") : t("Join with your player name")),
        h("div", { class: "row" }, h("div", { class: "grow" }, playerName), join),
        h("p", { class: "muted small" }, t("You get a game and options rolled from the round's weights when the game is generated."))) : null);
  }

  function entriesCard() {
    if (!signup.entries.length) return h("div", { class: "card" }, h("p", { class: "muted", style: { margin: 0 } }, t("No entries yet.")));
    const removable = manager && ["open", "closed"].includes(signup.status);
    return h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("Player"), t("Games"), t("Entered by"), t("When"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, signup.entries.map((e) => h("tr", {},
        h("td", {}, h("span", { class: "slot-name" }, e.player_names.join(", ") || h("span", { class: "muted" }, t("name from file"))),
          e.issues.map((issue) => h("div", { class: "form-error small" }, tServer(issue))), manager ? generationError(e, "small") : null),
        h("td", {}, chips(e.games, e.missing_games)),
        h("td", {}, e.username, e.mine ? h("span", { class: "muted" }, " ", t("(you)")) : null),
        h("td", { class: "muted" }, fmtAgo(e.created_at)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          manager ? h("a", { class: "btn sm", href: `/api/signups/${id}/entries/${e.id}/download` }, icon("download", 14)) : null,
          removable ? h("button", { class: "btn sm", type: "button", title: t("Remove entry"), "aria-label": t("Remove the entry of {user}", { user: e.username }), onclick: action(async () => {
            if (await confirmDialog(t("Remove the entry of {user}? Their config stays in their library.", { user: e.username }),
              { title: t("Remove entry?"), confirmText: t("Remove"), danger: true })) {
              await run("DELETE", `/api/signups/${id}/entries/${e.id}`);
            }
          }) }, icon("trash", 14)) : null)))))));
  }

  // What has to be on the server before the round can be generated, for whoever will generate it
  function managerWarnings() {
    if (!manager || !["open", "closed"].includes(signup.status)) return null;
    const missing = [...new Set(signup.entries.flatMap((e) => e.missing_games))];
    return [
      missing.length ? missingGamesPanel(missing.map((game) => ({ game, configs: [], games: [], pending: [] })), () => refresh(true)) : null,
      missingRomsPanel(signup.entries.map((e) => e.missing_roms)),
    ];
  }

  function draw() {
    clear(host).append(header(), h("div", { class: "stack" }, stateBanner(), testBanner(), managerWarnings(), rulesCard(), myCard(),
      h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Entries"))), entriesCard())));
  }

  signature = JSON.stringify(signup);
  draw();
  root.append(host);
  const timer = setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 5000);
  cleanup(() => clearInterval(timer));
}
