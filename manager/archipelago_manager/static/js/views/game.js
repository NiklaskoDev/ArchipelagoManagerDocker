import { h, clear, icon, toast, action, confirmDialog, fmtBytes, fmtAgo, fmtDate, shortName } from "../util.js";
import { api, get, post, patch, del, isAdmin, state } from "../api.js";
import { stateBadge, badge, chips, progressBar, copyField, copyButton, controlButtons, banner, field, checkbox, select,
         missingGamesPanel, isActive, serverOptionsForm } from "../components.js";
import { t, tServer, plural } from "../i18n.js";
import { textClientSection } from "../textclient.js";

// what a backup was made for, and the kind of a game file, as the server names them
const BACKUP_REASONS = { auto: t("automatic"), manual: t("made by hand"), "before-reset": t("before a reset"), "before-restore": t("before a restore") };
const FILE_KINDS = { patch: t("patch"), spoiler: t("spoiler"), yaml: t("yaml"), other: t("other") };

export default async function render({ root, params, cleanup, isCurrent, query }) {
  const id = Number(params[0]);
  const admin = isAdmin();
  let game = await get(`/api/games/${id}`);
  const users = admin ? await get("/api/users") : [];

  const headerHost = h("div");
  const infoHost = h("div");
  const trackerHost = h("div");
  const textClientHost = h("div");
  const slotsHost = h("div");
  const adminHost = h("div", { class: "stack section" });
  let drawnSignature = "";

  // -------------------------------------------------------------------------------------------------------------------
  // header
  // -------------------------------------------------------------------------------------------------------------------
  function drawHeader() {
    clear(headerHost).append(
      h("div", { class: "muted small" }, h("a", { href: "#/games" }, t("← All games"))),
      h("div", { class: "page-head", style: { marginTop: "8px" } },
        h("div", {},
          h("div", { class: "row" }, h("h1", {}, game.name), stateBadge(game.state), game.race_mode ? badge(t("Race mode"), "warn") : null),
          h("p", { class: "muted" }, [
            game.seed_name ? t("Seed {name}", { name: game.seed_name }) : null,
            game.source === "generated" ? t("generated here") : t("uploaded"),
            t("added {date}", { date: fmtDate(game.created_at) }),
          ].filter(Boolean).join(" · "))),
        h("div", { class: "page-actions" }, controlButtons(game, applyGame),
          // the same configs and options once more, with a new seed
          game.job_available && (admin || state.status?.permissions?.generate)
            ? h("a", { class: "btn sm", href: `#/generate?from=${game.job_id}` }, icon("restart", 14), t("Generate again")) : null)),
      game.state === "crashed" ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, t("The server stopped unexpectedly.")), " ", game.error ? tServer(game.error) : t("See the console for details."))) : null,
      game.missing_games.length ? missingGamesPanel(game.missing_games.map((g) => ({ game: g, configs: [], games: [{ id, name: game.name }], pending: [] })),
        refresh) : null);
  }

  function drawInfo() {
    const running = game.state === "running";
    clear(infoHost).append(h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, t("Connect")),
        !running && game.state !== "starting" ? h("span", { class: "muted small" }, t("The server is not running right now.")) : null),
      h("dl", { class: "kv" },
        h("dt", {}, t("Server")), h("dd", {}, game.address ? copyField(game.address) : h("span", { class: "muted" }, t("No port assigned"))),
        game.password ? [h("dt", {}, t("Password")), h("dd", {}, copyField(game.password))] : null,
        game.my_slots.length ? [h("dt", {}, plural(game.my_slots.length, "Your slot", "Your slots")),
          h("dd", { class: "stack tight" }, game.my_slots.map((s) => h("div", { class: "row" },
            copyField(s.name), h("span", { class: "muted" }, s.game))))] : null,
        h("dt", {}, t("Progress")), h("dd", {}, progressBar(game.progress.checked, game.progress.total))),
      h("p", { class: "muted small" }, game.password
        ? t("In your Archipelago client, enter the server address, your slot name and the password.")
        : t("In your Archipelago client, enter the server address and your slot name."))));
  }

  // -------------------------------------------------------------------------------------------------------------------
  // slots
  // -------------------------------------------------------------------------------------------------------------------
  function statusCell(slot) {
    if (slot.type === 2) return h("span", { class: "muted" }, t("Group"));
    if (slot.type !== 1) return h("span", { class: "muted" }, t("Spectator"));
    const kind = { online: "ok", goal: "ok", offline: "muted" }[slot.status];
    const label = { online: t("Online"), goal: t("Goal complete"), offline: slot.last_activity ? t("Seen {ago}", { ago: fmtAgo(slot.last_activity) }) : t("Offline") }[slot.status];
    return h("span", { class: "status-line" }, h("span", { class: `dot ${kind}` }), label);
  }

  function ownerCell(slot) {
    if (slot.type !== 1) return h("span", { class: "muted" }, "—");
    if (admin) {
      const sel = h("select", { class: "input", "aria-label": t("Owner of {slot}", { slot: slot.name }) },
        h("option", { value: "" }, t("— nobody —")),
        users.map((u) => h("option", { value: u.id, selected: u.id === slot.user_id }, u.username)));
      sel.onchange = async () => {
        try {
          const result = await post(`/api/games/${id}/slots/${slot.slot}/assign`, { user_id: sel.value ? Number(sel.value) : null });
          applyGame(result);
          if (result.configs_added) toast(plural(result.configs_added, "Copied {n} config file from this game into the user's library",
            "Copied {n} config files from this game into the user's library"), "success");
        } catch (err) { toast(err.message, "error"); applyGame(await get(`/api/games/${id}`)); }
      };
      return sel;
    }
    if (slot.mine) {
      return h("span", { class: "row" }, h("strong", {}, t("You")),
        h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => applyGame(await post(`/api/games/${id}/slots/${slot.slot}/unclaim`))) }, t("Release")));
    }
    if (slot.owner) return slot.owner;
    if (slot.claimable) {
      return h("button", { class: "btn sm primary", type: "button", onclick: action(async () => {
        const result = await post(`/api/games/${id}/slots/${slot.slot}/claim`);
        applyGame(result);
        toast(t("You now own {slot}", { slot: slot.name }), "success");
        if (result.configs_added) toast(t("Your YAML was added to your configs"), "success");
      }) }, t("This is mine"));
    }
    return h("span", { class: "muted" }, t("unclaimed"));
  }

  function drawSlots() {
    const rows = game.slots;
    const myFiles = rows.filter((s) => s.mine).flatMap((s) => s.files).length;
    clear(slotsHost).append(h("div", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, t("Slots")),
        myFiles > 1 ? h("a", { class: "btn sm", href: `/api/games/${id}/my-files.zip` }, icon("download", 14), t("Download all my files")) : null),
      h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["#", t("Player"), t("Game"), t("Owner"), t("Progress"), t("Status"), t("Files")].map((text) => h("th", {}, text)))),
        h("tbody", {}, rows.map((s) => h("tr", {},
          h("td", { class: "muted" }, s.slot),
          h("td", {}, h("span", { class: "slot-name" }, s.name), s.mine ? [" ", badge(t("you"), "accent")] : null),
          h("td", {}, s.game),
          h("td", {}, ownerCell(s)),
          h("td", {}, s.type === 1 ? progressBar(s.checked, s.locations, { compact: true }) : h("span", { class: "muted" }, "—")),
          h("td", {}, statusCell(s)),
          h("td", {}, s.files.length
            ? h("div", { class: "stack tight" }, s.files.map((f) => h("a", { class: "btn sm", href: `/api/games/${id}/files/${f.id}`, title: f.filename },
                icon("download", 14), shortName(f.filename), h("span", { class: "muted" }, fmtBytes(f.size)))))
            : h("span", { class: "muted" }, s.type === 1 ? (admin || s.mine ? t("no file") : "") : ""))))))),
      !admin && game.claimable && !game.my_slots.length
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, t("Find your slot above and press “{button}” to get your files.", { button: t("This is mine") }),
          game.single_claim ? ` ${t("Each player can claim one slot.")}` : "") : null,
      !admin && game.claimable && game.single_claim && game.my_slots.length && game.slots.some((s) => s.type === 1 && !s.user_id)
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, t("Each player can claim one slot. To swap, release yours first.")) : null));
  }

  function signature() {
    return JSON.stringify([game.state, game.error, game.slots, game.progress, game.password, game.address, game.missing_games, game.my_slots]);
  }
  function drawLive() {
    drawnSignature = signature();
    drawHeader();
    drawInfo();
    drawSlots();
    syncTracker();
    syncTextClient();
  }

  // The text client, for the player's own slots only: it speaks in their name
  let textClientKey = null, textClientCleanup = () => {};
  cleanup(() => textClientCleanup());
  function syncTextClient() {
    const slots = game.slots.filter((s) => s.type === 1 && s.mine);
    const key = slots.map((s) => s.slot).join(",");
    if (key === textClientKey) return;
    textClientKey = key;
    textClientCleanup();
    clear(textClientHost);
    if (!slots.length) return;
    const stops = [];
    textClientCleanup = () => stops.splice(0).forEach((fn) => fn());
    textClientHost.append(textClientSection({ gameId: id, slots, cleanup: (fn) => stops.push(fn), isCurrent }));
  }

  // -------------------------------------------------------------------------------------------------------------------
  // tracker: what a slot received, which of its locations are checked, and its hints
  // -------------------------------------------------------------------------------------------------------------------
  let trackerKey = null, trackerTimer = null;
  cleanup(() => clearInterval(trackerTimer));

  // Players get their own slots, admins every player slot; rebuilt only when that list changes (a claim, an assignment).
  function syncTracker() {
    const slots = game.slots.filter((s) => s.type === 1 && (admin || s.mine));
    const key = slots.map((s) => s.slot).join(",");
    if (key === trackerKey) return;
    trackerKey = key;
    clearInterval(trackerTimer);
    clear(trackerHost);
    if (slots.length) trackerHost.append(trackerSection(slots));
  }

  function trackerSection(slots) {
    let current = (slots.find((s) => s.mine) || slots[0]).slot;
    let tab = "items", data = null, loaded = "";
    const picker = slots.length > 1 ? select(slots.map((s) => [s.slot, `${s.name} (${s.game})${s.mine ? ` · ${t("you")}` : ""}`]), current) : null;
    if (picker) picker.style.maxWidth = "320px";
    const search = h("input", { class: "input", type: "search", placeholder: t("Filter…"), "aria-label": t("Filter the tracker") });
    const summary = h("div", { class: "muted small" });
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const content = h("div");
    const byName = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
    const matches = (...texts) => {
      const q = search.value.trim().toLowerCase();
      return !q || texts.some((text) => (text || "").toLowerCase().includes(q));
    };
    const itemLabel = (name, flags) => h("span", { class: flags & 1 ? "slot-name" : "" }, name,
      flags & 4 ? [" ", badge(t("trap"), "bad")] : flags & 1 ? null : flags & 2 ? [" ", badge(t("useful"), "muted")] : null);

    function itemsTab() {
      const groups = new Map();
      for (const it of data.items) {
        const g = groups.get(it.item) || { name: it.item, count: 0, flags: it.flags, last: it };
        g.count += 1;
        g.last = it;
        groups.set(it.item, g);
      }
      const rows = [...groups.values()].filter((g) => matches(g.name, g.last.from, g.last.location)).sort((a, b) => byName(a.name, b.name));
      if (!data.items.length) return h("p", { class: "muted" }, t("Nothing received yet."));
      return h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Item"), t("Count"), t("Last one from")].map((text) => h("th", {}, text)))),
        h("tbody", {}, rows.map((g) => h("tr", {},
          h("td", {}, itemLabel(g.name, g.flags)), h("td", {}, g.count > 1 ? `× ${g.count}` : ""),
          h("td", { class: "muted" }, g.last.start ? t("Starting inventory")
            : g.last.from === data.name ? t("Your own world · {location}", { location: g.last.location }) : `${g.last.from} · ${g.last.location}`))))));
    }

    function locationsTab() {
      const open = data.locations.filter((l) => !l.checked && matches(l.location)).map((l) => l.location).sort(byName);
      const done = data.locations.filter((l) => l.checked && matches(l.location)).map((l) => l.location).sort(byName);
      const list = (names) => h("div", { class: "chips" }, names.map((n) => h("span", { class: "chip" }, n)));
      return h("div", { class: "stack" },
        h("div", { class: "stack tight" }, h("strong", {}, t("Still to check ({n})", { n: open.length })),
          open.length ? list(open) : h("span", { class: "muted" }, data.locations.some((l) => !l.checked) ? t("Nothing matches.") : t("Everything is checked."))),
        done.length ? h("details", {}, h("summary", { class: "muted" }, t("Checked ({n})", { n: done.length })), h("div", { style: { marginTop: "8px" } }, list(done))) : null);
    }

    function hintsTab() {
      const label = { 0: t("Not found yet"), 10: t("No priority"), 20: t("Avoid"), 30: t("Priority") };
      const table = (hints, mine) => h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Item"), mine ? t("Is at") : t("For"), t("Status")].map((text) => h("th", {}, text)))),
        h("tbody", {}, hints.map((x) => h("tr", { style: x.found ? { opacity: 0.6 } : undefined },
          h("td", {}, itemLabel(x.item, x.flags)),
          h("td", {}, mine ? [x.location, h("div", { class: "muted small" }, x.finder === data.name ? t("in your own world") : t("in {player}'s world", { player: x.finder }),
            x.entrance ? ` · ${x.entrance}` : "")] : [x.receiver, h("div", { class: "muted small" }, t("at {location}", { location: x.location }), x.entrance ? ` · ${x.entrance}` : "")]),
          h("td", {}, x.found ? badge(t("Found"), "ok") : x.status === 30 ? badge(label[30], "warn") : h("span", { class: "muted" }, label[x.status] || label[0])))))));
      const forMe = data.hints.filter((x) => x.for_me && matches(x.item, x.location, x.finder, x.entrance));
      const inMine = data.hints.filter((x) => !x.for_me && x.in_my_world && matches(x.item, x.location, x.receiver, x.entrance));
      if (!data.hints.length) return h("p", { class: "muted" }, t("No hints yet. Ask for one in your client with !hint followed by an item name."));
      return h("div", { class: "stack" },
        h("div", { class: "stack tight" }, h("strong", {}, t("Where your items are")), forMe.length ? table(forMe, true) : h("span", { class: "muted" }, t("None."))),
        h("div", { class: "stack tight" }, h("strong", {}, t("Other players' items in your world")), inMine.length ? table(inMine, false) : h("span", { class: "muted" }, t("None."))));
    }

    function draw() {
      if (!data) return;
      const checked = data.locations.filter((l) => l.checked).length;
      const openHints = data.hints.filter((x) => !x.found && x.for_me).length;
      clear(summary).append([
        plural(data.items.length, "{n} item received", "{n} items received"),
        plural(data.locations.length, "{checked} of {n} location checked", "{checked} of {n} locations checked", { checked }),
        plural(openHints, "{n} open hint", "{n} open hints"),
      ].join(" · "), data.goal ? [" · ", badge(t("Goal complete"), "ok")] : null);
      const labels = { items: t("Items ({n})", { n: data.items.length }),
        locations: t("Locations ({n})", { n: `${checked}/${data.locations.length}` }), hints: t("Hints ({n})", { n: data.hints.length }) };
      clear(tabs).append(["items", "locations", "hints"].map((key) => h("button", { class: "tab", type: "button", role: "tab",
        "aria-selected": String(key === tab), "aria-current": key === tab ? "page" : undefined,
        onclick: () => { tab = key; draw(); } }, labels[key])));
      clear(content).append({ items: itemsTab, locations: locationsTab, hints: hintsTab }[tab]());
    }

    async function load() {
      try {
        const fresh = await get(`/api/games/${id}/slots/${current}/tracker`);
        const sig = JSON.stringify(fresh);
        if (!isCurrent() || sig === loaded) return;
        loaded = sig;
        data = fresh;
        draw();
      } catch (err) {
        clear(content).append(banner("error", icon("alert", 16), err.message));
      }
    }
    search.oninput = draw;
    if (picker) picker.onchange = () => { current = Number(picker.value); loaded = ""; data = null; clear(content).append(h("span", { class: "muted" }, t("Loading…"))); load(); };
    content.append(h("span", { class: "muted" }, t("Loading…")));
    load();
    // the server reads the save again only when it changed, so asking often is cheap
    trackerTimer = setInterval(() => { if (!document.hidden) load(); }, 10000);
    return h("div", { class: "section" }, h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, admin && !slots.some((s) => s.mine) ? t("Tracker") : t("Your tracker")), picker),
      summary, h("div", { class: "row spread" }, tabs, h("div", { style: { minWidth: "200px" } }, search)), content,
      h("p", { class: "muted small" }, t("Read from the server's save, so it can be up to a minute behind the game."))));
  }
  function applyGame(next) {
    game = next;
    if (signature() !== drawnSignature) drawLive();
  }
  async function refresh() {
    const next = await get(`/api/games/${id}`);
    if (isCurrent()) applyGame(next);
  }

  // -------------------------------------------------------------------------------------------------------------------
  // administration
  // -------------------------------------------------------------------------------------------------------------------
  function settingsSection() {
    const opts = game.options;
    const name = h("input", { class: "input", value: game.name, maxLength: 120 });
    const port = h("input", { class: "input", type: "number", min: 1024, max: 65535, value: game.port ?? "" });
    const password = h("input", { class: "input", value: game.password ?? "", placeholder: t("none"), autocomplete: "off" });
    const adminPassword = h("input", { class: "input", value: game.server_password ?? "", placeholder: t("none (no remote admin)"), autocomplete: "off" });
    const visibility = select([["participants", t("Only players in the game")], ["all", t("Every user on this server")]], game.visibility);
    const singleClaim = checkbox(t("Players may claim only one slot each"), game.single_claim,
      t("Administrators can still assign more slots to someone by hand."));
    const claimable = checkbox(t("Let users claim unowned slots themselves"), game.claimable,
      t("Handy when you upload a seed and your friends should pick their own slot."));
    const notes = h("textarea", { class: "input", rows: 2, maxLength: 2000, placeholder: t("Only visible to administrators") }, game.notes ?? "");
    const rules = serverOptionsForm(opts);
    const save = h("button", { class: "btn primary", type: "button" }, t("Save settings"));
    save.onclick = action(async () => {
      const options = rules.read();
      const body = { name: name.value, password: password.value, server_password: adminPassword.value, options,
        visibility: visibility.value, claimable: claimable.input.checked, single_claim: singleClaim.input.checked,
        notes: notes.value };
      if (port.value) body.port = Number(port.value);
      const result = await patch(`/api/games/${id}`, body);
      game = result;
      drawLive();
      toast(result.restart_required ? t("Saved. Restart the server to apply the port or startup options.") : t("Settings saved"), "success");
    });
    return h("details", { class: "card" }, h("summary", {}, t("Settings")),
      h("div", { class: "stack" },
        h("div", { class: "form-grid" },
          field(t("Name"), name), field(t("Port"), port, t("Must be mapped in Docker. Takes effect after a restart.")),
          field(t("Room password"), password, t("Players need it to connect")),
          field(t("Remote admin password"), adminPassword, t("Lets clients use !admin; leave empty to disable")),
          field(t("Who can see this game"), visibility)),
        claimable, singleClaim, field(t("Notes"), notes),
        h("h3", {}, t("Server rules")), rules.node,
        h("div", { class: "row" }, save)));
  }

  function filesSection() {
    const all = [...game.slots.flatMap((s) => s.files), ...game.shared_files];
    const slotChoices = [["", t("— none —")], ...game.slots.filter((s) => s.type === 1).map((s) => [s.slot, `#${s.slot} ${s.name}`])];
    const input = h("input", { type: "file", class: "input" });
    const slotSel = select(slotChoices, "");
    const pub = checkbox(t("Visible to every player in the game"), false);
    const upload = h("button", { class: "btn", type: "button" }, icon("upload", 14), t("Add file"));
    upload.onclick = action(async () => {
      if (!input.files[0]) throw new Error(t("Choose a file first"));
      const form = new FormData();
      form.append("file", input.files[0]);
      if (slotSel.value) form.append("slot", slotSel.value);
      form.append("public", pub.input.checked ? "true" : "false");
      game = await api("POST", `/api/games/${id}/files`, form);
      drawLive();
      redrawFiles();
      toast(t("File added"), "success");
    });
    const host = h("div", { class: "stack" });
    const kindBadge = { patch: "accent", spoiler: "warn", yaml: "muted", other: "muted" };
    const redrawFiles = () => {
      const files = [...game.slots.flatMap((s) => s.files), ...game.shared_files];
      clear(host).append(
        files.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, [t("File"), t("Type"), t("Assigned to"), t("Players can see"), "", ""].map((text) => h("th", {}, text)))),
          h("tbody", {}, files.map((f) => {
            const sel = select(slotChoices, f.slot ?? "");
            sel.onchange = action2(async () => { game = await patch(`/api/games/${id}/files/${f.id}`, { slot: sel.value === "" ? null : Number(sel.value) }); drawLive(); redrawFiles(); });
            const pubBox = h("input", { type: "checkbox", checked: f.public, "aria-label": t("Visible to everyone"),
              onchange: action2(async () => { game = await patch(`/api/games/${id}/files/${f.id}`, { public: pubBox.checked }); drawLive(); redrawFiles(); }) });
            return h("tr", {},
              h("td", {}, h("span", { class: "mono" }, f.filename), h("div", { class: "muted small" }, fmtBytes(f.size))),
              h("td", {}, badge(FILE_KINDS[f.kind] || f.kind, kindBadge[f.kind])), h("td", {}, sel),
              h("td", {}, f.kind === "patch" ? h("span", { class: "muted small" }, t("its owner")) : null, pubBox),
              h("td", {}, h("a", { class: "btn sm ghost icon-only", href: `/api/games/${id}/files/${f.id}`, "aria-label": t("Download") }, icon("download", 14))),
              h("td", {}, h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": t("Delete"),
                onclick: action(async () => { if (await confirmDialog(t("Delete {name}?", { name: f.filename }), { danger: true, confirmText: t("Delete") })) { game = await del(`/api/games/${id}/files/${f.id}`); drawLive(); redrawFiles(); } }) }, icon("trash", 14))));
          })))) : h("p", { class: "muted" }, t("No files.")));
    };
    redrawFiles();
    return h("details", { class: "card" }, h("summary", {}, t("Files ({n})", { n: all.length })),
      h("div", { class: "stack" },
        h("p", { class: "muted small" }, t("Patch files go to the owner of the slot they are assigned to. Spoiler logs and YAMLs stay hidden unless you share them.")),
        host, h("hr"),
        h("div", { class: "form-grid" }, field(t("Add a file"), input), field(t("Assign to slot"), slotSel)), pub,
        h("div", { class: "row" }, upload,
          h("a", { class: "btn", href: `/api/games/${id}/multidata` }, icon("download", 14), t("Download multiworld data")))));
  }

  function consoleSection() {
    const out = h("div", { class: "console", tabIndex: 0, "aria-label": t("Server log") });
    const input = h("input", { class: "input mono", placeholder: t("Server command, e.g. /send Alice Hammer or /help"), autocomplete: "off" });
    let after = 0, timer = null, polling = false;
    const addLines = (lines) => {
      const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
      for (const line of lines) {
        const cls = line.text.startsWith("> ") ? "cmd" : line.text.startsWith("---") ? "sys" : /\b(error|exception|traceback)\b/i.test(line.text) ? "err" : "";
        out.append(h("div", { class: cls }, line.text));
      }
      while (out.childElementCount > 1500) out.firstChild.remove();
      if (pinned) out.scrollTop = out.scrollHeight;
    };
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const r = await get(`/api/games/${id}/log?after=${after}`);
        if (r.lines.length) addLines(r.lines);
        after = r.last;
      } catch { /* the next tick retries */ } finally { polling = false; }
    };
    const send = async () => {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      try { await post(`/api/games/${id}/command`, { command: text }); setTimeout(poll, 300); } catch (err) { toast(err.message, "error"); input.value = text; }
    };
    input.onkeydown = (e) => { if (e.key === "Enter") send(); };
    const details = h("details", { class: "card" }, h("summary", {}, t("Console")),
      h("div", { class: "stack" }, out,
        h("div", { class: "row" }, h("div", { class: "grow" }, input), h("button", { class: "btn", type: "button", onclick: send }, t("Send"))),
        h("p", { class: "muted small" }, t("Commands run on the server console. Type /help to list them."))));
    details.addEventListener("toggle", () => {
      clearInterval(timer);
      if (details.open) { poll(); timer = setInterval(() => { if (!document.hidden) poll(); }, 1500); }
    });
    cleanup(() => clearInterval(timer));
    return details;
  }

  function backupsSection() {
    const host = h("div", { class: "stack" });
    const draw = (data) => {
      clear(host).append(
        h("p", { class: "muted small" }, t("While the save changes, a copy is kept every {minutes} minutes (the latest {keep}), and one before every reset or restore. Restoring needs the server stopped; the current progress is kept as a backup first.",
          { minutes: data.interval_minutes, keep: data.keep_auto })),
        data.backups.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, [t("Progress as of"), t("Kind"), t("Size"), ""].map((text) => h("th", {}, text)))),
          h("tbody", {}, data.backups.map((b) => h("tr", {},
            h("td", {}, fmtDate(b.saved_at), h("div", { class: "muted small" }, fmtAgo(b.saved_at))),
            h("td", {}, BACKUP_REASONS[b.reason] || b.reason),
            h("td", { class: "muted" }, fmtBytes(b.size)),
            h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
              h("button", { class: "btn sm", type: "button", disabled: isActive(game), title: isActive(game) ? t("Stop the server first") : undefined,
                onclick: action(async () => {
                  if (!(await confirmDialog(t("The game goes back to its progress of {date}. The current progress is kept as a backup.", { date: fmtDate(b.saved_at) }),
                    { title: t("Restore this backup?"), confirmText: t("Restore") }))) return;
                  applyGame(await post(`/api/games/${id}/backups/${encodeURIComponent(b.name)}/restore`));
                  toast(t("Backup restored. Start the server to continue from there."), "success");
                  await load();
                }) }, icon("restart", 14), t("Restore")),
              h("a", { class: "btn sm ghost icon-only", href: `/api/games/${id}/backups/${encodeURIComponent(b.name)}`, title: t("Download"), "aria-label": t("Download") }, icon("download", 14)),
              h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Delete"), "aria-label": t("Delete"), onclick: action(async () => {
                if (await confirmDialog(t("Delete this backup?"), { danger: true, confirmText: t("Delete") })) draw(await del(`/api/games/${id}/backups/${encodeURIComponent(b.name)}`));
              }) }, icon("trash", 14))))))))) : h("p", { class: "muted" }, data.has_save ? t("No backups yet.") : t("No backups yet: the game has no saved progress.")),
        h("div", { class: "row" }, h("button", { class: "btn", type: "button", disabled: !data.has_save, onclick: action(async () => {
          draw(await post(`/api/games/${id}/backups`));
          toast(t("Backup made"), "success");
        }) }, icon("download", 14), t("Back up now"))));
    };
    const load = async () => draw(await get(`/api/games/${id}/backups`));
    const details = h("details", { class: "card" }, h("summary", {}, t("Backups")), host);
    details.addEventListener("toggle", () => { if (details.open) load().catch((err) => toast(err.message, "error")); });
    return details;
  }

  function dangerSection() {
    return h("details", { class: "card" }, h("summary", {}, t("Danger zone")),
      h("div", { class: "stack" },
        h("div", { class: "row spread" },
          h("div", {}, h("strong", {}, t("Reset progress")), h("div", { class: "muted small" }, t("Deletes the server's save so the game starts from zero. The server must be stopped. A backup is made first, so this can be undone under Backups."))),
          h("button", { class: "btn danger", type: "button", disabled: isActive(game) || !game.has_save, onclick: action(async () => {
            if (!(await confirmDialog(t("All progress, received items and hints go back to zero. A backup of the current progress is made first."), { title: t("Reset progress?"), danger: true, confirmText: t("Reset") }))) return;
            applyGame(await post(`/api/games/${id}/reset-save`));
            toast(t("Progress reset"), "success");
          }) }, t("Reset progress"))),
        h("div", { class: "row spread" },
          h("div", {}, h("strong", {}, t("Delete this game")), h("div", { class: "muted small" }, t("Removes the multiworld, its save and all files for every player."))),
          h("button", { class: "btn danger", type: "button", onclick: action(async () => {
            if (!(await confirmDialog(t("“{name}” and everything stored for it will be deleted.", { name: game.name }), { title: t("Delete game?"), danger: true, confirmText: t("Delete game") }))) return;
            await del(`/api/games/${id}`);
            toast(t("Game deleted"), "success");
            location.hash = "#/games";
          }) }, icon("trash", 14), t("Delete game")))));
  }

  // -------------------------------------------------------------------------------------------------------------------
  root.append(headerHost, infoHost, trackerHost, textClientHost, slotsHost, adminHost);
  drawLive();
  if (admin) {
    adminHost.append(settingsSection(), filesSection(), consoleSection(), backupsSection(), dangerSection());
    if (query.get("tab") === "console") adminHost.querySelector("details:nth-of-type(3)")?.setAttribute("open", "");
  }
  const timer = setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 4000);
  cleanup(() => clearInterval(timer));
}

// change handlers on plain elements still need error toasts
function action2(fn) {
  return async (event) => {
    try { await fn(event); } catch (err) { toast(err.message, "error"); }
  };
}
