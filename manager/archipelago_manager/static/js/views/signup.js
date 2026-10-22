import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo, fmtDate, fmtUntil, toLocalInput, fromLocalInput } from "../util.js";
import { api, get, post, patch, del, isAdmin } from "../api.js";
import { banner, badge, chips, field, checkbox, select, missingGamesPanel, promptMissingGames } from "../components.js";

const SPOILER_CHOICES = [[1, "Basic spoiler log"], [0, "No spoiler log"], [2, "With playthrough"], [3, "Playthrough and paths"]];
const PLANDO = [["bosses", "Bosses"], ["connections", "Connections"], ["texts", "Texts"], ["items", "Items"]];

export function signupBadge(signup) {
  if (signup.status === "open") return signup.open ? badge("Open", "ok") : badge("Closing…", "warn");
  return { closed: badge("Closed", "muted"), generating: badge("Generating…", "warn"), done: badge("Game ready", "ok") }[signup.status];
}

export function deadlineText(signup) {
  if (signup.status !== "open") return signup.closed_at ? `Closed ${fmtDate(signup.closed_at)}` : null;
  return signup.deadline ? `Closes ${fmtDate(signup.deadline)} (${fmtUntil(signup.deadline)})` : "No deadline: an administrator closes it";
}

// Create (no `signup`) or edit a round. `onSaved` receives the round as the server returns it.
export async function openSignupForm({ signup = null, onSaved }) {
  const worlds = await get("/api/worlds").catch(() => ({ games: [] }));
  const known = worlds.games.map((g) => g.name);
  const opts = signup?.options || { spoiler: 1, race: false, skip_prog_balancing: false, plando: ["bosses", "connections", "texts"], host_after: true };

  const name = h("input", { class: "input", maxLength: 120, value: signup?.name || "", placeholder: "e.g. Friday run" });
  const description = h("textarea", { class: "input", rows: 3, maxLength: 2000, value: signup?.description || "",
    placeholder: "Optional: what this round is about, when you play, house rules…" });
  const deadline = h("input", { class: "input", type: "datetime-local", value: toLocalInput(signup?.deadline) });
  const noDeadline = checkbox("No deadline: close the sign-ups by hand", signup ? !signup.deadline : false);
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
  const gameInput = h("input", { class: "input", list: listId, placeholder: "Type a game and press Enter" });
  const drawAllowed = () => {
    clear(chipsHost).append(allowed.length ? allowed.map((g) => h("span", { class: "chip" }, g,
      h("button", { class: "btn ghost icon-only sm", type: "button", "aria-label": `Remove ${g}`,
        onclick: () => { allowed.splice(allowed.indexOf(g), 1); drawAllowed(); } }, icon("x", 12))))
      : h("span", { class: "muted small" }, "Any game installed on this server"));
  };
  const addGame = () => {
    const value = gameInput.value.trim();
    if (!value) return;
    const match = known.find((g) => g.toLowerCase() === value.toLowerCase());
    if (!match) { toast(`"${value}" is not installed on this server`, "error"); return; }
    if (!allowed.includes(match)) allowed.push(match);
    gameInput.value = "";
    drawAllowed();
  };
  gameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addGame(); } });
  drawAllowed();

  const autoGenerate = checkbox("Generate the game automatically when sign-ups close", signup ? signup.auto_generate : true);
  const hostAfter = checkbox("Start the server once the game is generated", opts.host_after);
  const spoiler = select(SPOILER_CHOICES, opts.spoiler);
  const race = checkbox("Race mode", opts.race, "Spoiler log is withheld and patches are encrypted where supported.");
  const skipBalance = checkbox("Skip progression balancing", opts.skip_prog_balancing);
  const plando = PLANDO.map(([key, label]) => ({ key, box: checkbox(label, opts.plando.includes(key)) }));

  const save = h("button", { class: "btn primary", type: "button" }, signup ? "Save" : "Open sign-ups");
  save.onclick = action(async () => {
    let when = null;
    if (!noDeadline.input.checked) {
      when = fromLocalInput(deadline.value);
      if (!when) throw new Error("Pick a deadline, or choose “No deadline”");
    }
    const body = {
      name: name.value, description: description.value, deadline: when, configs_per_user: Number(perUser.value),
      allowed_games: allowed, auto_generate: autoGenerate.input.checked,
      options: { spoiler: Number(spoiler.value), race: race.input.checked, skip_prog_balancing: skipBalance.input.checked,
        plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), host_after: hostAfter.input.checked },
    };
    const result = signup ? await patch(`/api/signups/${signup.id}`, body) : await post("/api/signups", body);
    modal.close();
    toast(signup ? "Round saved" : "Sign-ups are open", "success");
    onSaved?.(result);
  });

  const modal = openModal({
    title: signup ? "Edit round" : "New sign-up round", wide: true,
    body: h("div", { class: "stack" },
      field("Name", name, "Also the name of the generated game"),
      field("Description", description),
      h("div", { class: "form-grid" },
        field("Deadline", deadline, signup?.status === "closed" ? "Reopen the round to take entries again." : "Sign-ups close automatically at this time."),
        field("Configs per player", perUser)),
      noDeadline,
      field("Allowed games", h("div", { class: "stack tight" }, chipsHost, h("div", { class: "row" }, h("div", { class: "grow" }, gameInput),
        h("button", { class: "btn sm", type: "button", onclick: addGame }, icon("plus", 14), "Add"))),
        "Leave empty to allow every installed game."),
      h("datalist", { id: listId }, known.map((g) => h("option", { value: g }))),
      h("div", { class: "stack tight" }, autoGenerate, hostAfter),
      h("details", {}, h("summary", { class: "muted" }, "Generation options"),
        h("div", { class: "stack", style: { marginTop: "10px" } },
          field("Spoiler log", spoiler), h("div", { class: "stack tight" }, race, skipBalance),
          h("div", { class: "stack tight" }, h("span", { class: "field-label" }, "Allowed plando"), h("div", { class: "row" }, plando.map((p) => p.box)))))),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), save],
  });
}

export default async function render({ root, params, cleanup, isCurrent }) {
  const id = Number(params[0]);
  const admin = isAdmin();
  let signup = await get(`/api/signups/${id}`);
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
    for (const warning of result.warnings || []) toast(warning, "info", 9000);
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
    if (admin) {
      actions.push(h("button", { class: "btn", type: "button", disabled: !["open", "closed"].includes(signup.status),
        onclick: action(() => openSignupForm({ signup, onSaved: apply })) }, icon("edit", 14), "Edit"));
      if (signup.status === "open") {
        actions.push(h("button", { class: "btn", type: "button", onclick: action(async () => {
          const text = signup.auto_generate && signup.entry_count ? "Nobody can enter after this, and the game is generated right away." : "Nobody can enter after this.";
          if (await confirmDialog(text, { title: "Close sign-ups now?", confirmText: "Close sign-ups" })) await run("POST", `/api/signups/${id}/close`);
        }) }, icon("stop", 14), "Close now"));
      }
      if (signup.status === "closed") {
        actions.push(h("button", { class: "btn", type: "button", onclick: action(reopenDialog) }, icon("restart", 14), "Reopen"));
        actions.push(h("button", { class: "btn primary", type: "button", disabled: !signup.entry_count,
          onclick: action(() => run("POST", `/api/signups/${id}/generate`)) }, icon("zap", 14), "Generate"));
      }
      actions.push(h("button", { class: "btn danger", type: "button", disabled: signup.status === "generating", onclick: action(async () => {
        if (!(await confirmDialog("The entries are removed. Configs in the players' libraries and a generated game stay.",
          { title: "Delete this round?", confirmText: "Delete", danger: true }))) return;
        await del(`/api/signups/${id}`);
        toast("Round deleted", "success");
        location.hash = "#/signups";
      }) }, icon("trash", 14), "Delete"));
    }
    return h("div", {},
      h("div", { class: "muted small" }, h("a", { href: "#/signups" }, "← All sign-ups")),
      h("div", { class: "page-head", style: { marginTop: "8px" } },
        h("div", {}, h("div", { class: "row" }, h("h1", {}, signup.name), signupBadge(signup)),
          h("p", { class: "muted" }, [deadlineText(signup), signup.creator ? `by ${signup.creator}` : null].filter(Boolean).join(" · "))),
        h("div", { class: "page-actions" }, actions)));
  }

  function reopenDialog() {
    const deadline = h("input", { class: "input", type: "datetime-local" });
    const d = new Date(Date.now() + 2 * 86400000);
    d.setHours(20, 0, 0, 0);
    deadline.value = toLocalInput(d.getTime() / 1000);
    const noDeadline = checkbox("No deadline", false);
    noDeadline.input.addEventListener("change", () => { deadline.disabled = noDeadline.input.checked; });
    const go = h("button", { class: "btn primary", type: "button" }, "Reopen");
    go.onclick = action(async () => {
      const when = noDeadline.input.checked ? null : fromLocalInput(deadline.value);
      if (!noDeadline.input.checked && !when) throw new Error("Pick a deadline");
      await run("POST", `/api/signups/${id}/reopen`, { deadline: when });
      modal.close();
    });
    const modal = openModal({ title: "Reopen sign-ups", body: h("div", { class: "stack" }, field("New deadline", deadline), noDeadline),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), go] });
  }

  function stateBanner() {
    if (signup.status === "open" && signup.open) {
      return banner("info", icon("calendar", 18), h("div", { class: "grow" },
        h("strong", {}, "Sign-ups are open. "),
        signup.deadline ? `Enter your config ${fmtUntil(signup.deadline)}, until ${fmtDate(signup.deadline)}.` : "Enter your config until an administrator closes the round.",
        h("div", { class: "small muted" }, "You can still change or withdraw your entry until then.")));
    }
    if (signup.status === "generating") {
      return banner("info", h("span", { class: "spinner" }), h("div", { class: "grow" }, h("strong", {}, "Generating the game…"),
        admin && signup.job_id ? [" ", h("a", { href: `#/jobs/${signup.job_id}` }, "Show the log")] : null));
    }
    if (signup.status === "done") {
      return banner("ok", icon("check", 18), h("div", { class: "grow" }, h("strong", {}, "The game is ready. "),
        "Everyone who entered was added to it."), signup.game_id ? h("a", { class: "btn sm primary", href: `#/games/${signup.game_id}` }, "Open the game") : null);
    }
    if (signup.status === "closed" || !signup.open) {
      return [
        banner("warn", icon("alert", 18), h("div", { class: "grow" }, h("strong", {}, "Sign-ups are closed. "),
          admin ? "Generate the game when you are ready, or reopen the round." : "The game will be generated soon.")),
        admin && signup.error ? banner("error", icon("alert", 18), h("div", { class: "grow" }, h("strong", {}, "Last attempt failed: "), signup.error)) : null,
      ];
    }
    return null;
  }

  function rulesCard() {
    const o = signup.options;
    const extras = [o.race ? "race mode" : null, `spoiler level ${o.spoiler}`, o.host_after ? "server starts after generating" : null,
      signup.auto_generate ? "generated as soon as sign-ups close" : "generated by an administrator"].filter(Boolean);
    return h("div", { class: "card stack" },
      signup.description ? h("p", { style: { whiteSpace: "pre-wrap", margin: 0 } }, signup.description) : null,
      h("dl", { class: "kv" },
        h("dt", {}, "Games"), h("dd", {}, signup.allowed_games.length ? chips(signup.allowed_games) : h("span", { class: "muted" }, "Any game installed on this server")),
        h("dt", {}, "Configs per player"), h("dd", {}, String(signup.configs_per_user)),
        h("dt", {}, "Entries"), h("dd", {}, `${signup.entry_count} from ${signup.user_count} ${signup.user_count === 1 ? "player" : "players"}`),
        h("dt", {}, "Settings"), h("dd", { class: "muted" }, extras.join(" · "))));
  }

  // ---------------------------------------------------------------------------------------------------------------------
  function myCard() {
    const mine = signup.entries.filter((e) => e.mine);
    // Admins can still fix entries after the deadline, until the game is generated
    const editable = signup.open || (admin && signup.status === "closed");
    const canEnter = editable && mine.length < signup.configs_per_user;
    if (!mine.length && !editable) return null;
    const entered = new Set(mine.map((e) => e.config_id));
    const choices = configs.filter((c) => !entered.has(c.id));
    const missing = [...new Set(mine.flatMap((e) => e.missing_games))];

    const picker = choices.length ? select(choices.map((c) => [c.id, `${c.name} (${c.player_names.join(", ") || "name from file"} · ${c.games.join(", ")})`]), choices[0].id) : null;
    const enter = picker ? h("button", { class: "btn primary", type: "button",
      onclick: action(() => run("POST", `/api/signups/${id}/entries`, { config_id: Number(picker.value) })) }, icon("check", 14), "Enter") : null;
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
        toast("Uploaded to your configs and entered", "success");
      } catch (err) { toast(err.message, "error"); draw(); }
    };

    return h("div", { class: "card stack" },
      h("h2", {}, mine.length > 1 ? "Your entries" : "Your entry"),
      mine.length ? h("div", { class: "stack" }, mine.map((e) => h("div", { class: "stack tight" },
        h("div", { class: "row spread" },
          h("div", {}, h("span", { class: "slot-name" }, e.player_names.join(", ") || e.name), " ", chips(e.games, e.missing_games),
            h("div", { class: "muted small" }, `${e.name} · entered ${fmtAgo(e.created_at)}`, e.updated_at > e.created_at + 1 ? `, updated ${fmtAgo(e.updated_at)}` : "")),
          h("div", { class: "row" },
            h("a", { class: "btn sm", href: `/api/signups/${id}/entries/${e.id}/download` }, icon("download", 14), "YAML"),
            signup.open ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              if (await confirmDialog("Your config stays in your library.", { title: "Withdraw this entry?", confirmText: "Withdraw" })) await run("DELETE", `/api/signups/${id}/entries/${e.id}`);
            }) }, "Withdraw") : null)),
        e.outdated && editable ? banner("info", icon("edit", 16), h("div", { class: "grow" }, "You changed this config after entering it. The round still has the old version."),
          h("button", { class: "btn sm primary", type: "button", onclick: action(() => run("POST", `/api/signups/${id}/entries/${e.id}/refresh`)) }, "Use the new version")) : null,
        e.issues.map((issue) => h("div", { class: "form-error" }, issue))))) : h("p", { class: "muted" }, "You have not entered yet."),
      missing.length ? missingGamesPanel(missing.map((game) => ({ game, configs: [], games: [], pending: [] })), reloadConfigs) : null,
      canEnter ? h("div", { class: "stack tight" },
        h("span", { class: "field-label" }, mine.length ? "Enter another config" : "Enter with one of your configs"),
        h("div", { class: "row" }, picker ? h("div", { class: "grow" }, picker) : h("span", { class: "muted" }, configs.length ? "You have no other configs." : "You have no configs yet."), enter,
          h("button", { class: "btn", type: "button", onclick: () => file.click() }, icon("upload", 14), "Upload a YAML"),
          h("a", { class: "btn ghost", href: "#/configs/new" }, "Create from a template")), file) : null);
  }

  function entriesCard() {
    if (!signup.entries.length) return h("div", { class: "card" }, h("p", { class: "muted", style: { margin: 0 } }, "No entries yet."));
    const removable = admin && ["open", "closed"].includes(signup.status);
    return h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Player", "Games", "Entered by", "When", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, signup.entries.map((e) => h("tr", {},
        h("td", {}, h("span", { class: "slot-name" }, e.player_names.join(", ") || h("span", { class: "muted" }, "name from file")),
          e.issues.map((issue) => h("div", { class: "form-error small" }, issue))),
        h("td", {}, chips(e.games, e.missing_games)),
        h("td", {}, e.username, e.mine ? h("span", { class: "muted" }, " (you)") : null),
        h("td", { class: "muted" }, fmtAgo(e.created_at)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          admin ? h("a", { class: "btn sm", href: `/api/signups/${id}/entries/${e.id}/download` }, icon("download", 14)) : null,
          removable ? h("button", { class: "btn sm", type: "button", title: "Remove entry", "aria-label": `Remove the entry of ${e.username}`, onclick: action(async () => {
            if (await confirmDialog(`Remove the entry of ${e.username}? Their config stays in their library.`, { title: "Remove entry?", confirmText: "Remove", danger: true })) {
              await run("DELETE", `/api/signups/${id}/entries/${e.id}`);
            }
          }) }, icon("trash", 14)) : null)))))));
  }

  function draw() {
    clear(host).append(header(), h("div", { class: "stack" }, stateBanner(), rulesCard(), myCard(),
      h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Entries")), entriesCard())));
  }

  signature = JSON.stringify(signup);
  draw();
  root.append(host);
  const timer = setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 5000);
  cleanup(() => clearInterval(timer));
}
