import { h, clear, icon, toast, action, confirmDialog, fmtBytes, fmtAgo, fmtDate, shortName } from "../util.js";
import { api, get, post, patch, del, isAdmin } from "../api.js";
import { stateBadge, badge, chips, progressBar, copyField, copyButton, controlButtons, banner, field, checkbox, select,
         missingGamesPanel, isActive, serverOptionsForm } from "../components.js";

export default async function render({ root, params, cleanup, isCurrent, query }) {
  const id = Number(params[0]);
  const admin = isAdmin();
  let game = await get(`/api/games/${id}`);
  const users = admin ? await get("/api/users") : [];

  const headerHost = h("div");
  const infoHost = h("div");
  const slotsHost = h("div");
  const adminHost = h("div", { class: "stack section" });
  let drawnSignature = "";

  // -------------------------------------------------------------------------------------------------------------------
  // header
  // -------------------------------------------------------------------------------------------------------------------
  function drawHeader() {
    clear(headerHost).append(
      h("div", { class: "muted small" }, h("a", { href: "#/games" }, "← All games")),
      h("div", { class: "page-head", style: { marginTop: "8px" } },
        h("div", {},
          h("div", { class: "row" }, h("h1", {}, game.name), stateBadge(game.state), game.race_mode ? badge("Race mode", "warn") : null),
          h("p", { class: "muted" }, [
            game.seed_name ? `Seed ${game.seed_name}` : null,
            game.source === "generated" ? "generated here" : "uploaded",
            `added ${fmtDate(game.created_at)}`,
          ].filter(Boolean).join(" · "))),
        h("div", { class: "page-actions" }, controlButtons(game, applyGame))),
      game.state === "crashed" ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, "The server stopped unexpectedly. "), game.error || "See the console for details.")) : null,
      game.missing_games.length ? missingGamesPanel(game.missing_games.map((g) => ({ game: g, configs: [], games: [{ id, name: game.name }], pending: [] })),
        refresh) : null);
  }

  function drawInfo() {
    const running = game.state === "running";
    clear(infoHost).append(h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, "Connect"),
        !running && game.state !== "starting" ? h("span", { class: "muted small" }, "The server is not running right now.") : null),
      h("dl", { class: "kv" },
        h("dt", {}, "Server"), h("dd", {}, game.address ? copyField(game.address) : h("span", { class: "muted" }, "No port assigned")),
        game.password ? [h("dt", {}, "Password"), h("dd", {}, copyField(game.password))] : null,
        game.my_slots.length ? [h("dt", {}, game.my_slots.length > 1 ? "Your slots" : "Your slot"),
          h("dd", { class: "stack tight" }, game.my_slots.map((s) => h("div", { class: "row" },
            copyField(s.name), h("span", { class: "muted" }, s.game))))] : null,
        h("dt", {}, "Progress"), h("dd", {}, progressBar(game.progress.checked, game.progress.total))),
      h("p", { class: "muted small" }, "In your Archipelago client, enter the server address, your slot name",
        game.password ? " and the password." : ".")));
  }

  // -------------------------------------------------------------------------------------------------------------------
  // slots
  // -------------------------------------------------------------------------------------------------------------------
  function statusCell(slot) {
    if (slot.type === 2) return h("span", { class: "muted" }, "Group");
    if (slot.type !== 1) return h("span", { class: "muted" }, "Spectator");
    const kind = { online: "ok", goal: "ok", offline: "muted" }[slot.status];
    const label = { online: "Online", goal: "Goal complete", offline: slot.last_activity ? `Seen ${fmtAgo(slot.last_activity)}` : "Offline" }[slot.status];
    return h("span", { class: "status-line" }, h("span", { class: `dot ${kind}` }), label);
  }

  function ownerCell(slot) {
    if (slot.type !== 1) return h("span", { class: "muted" }, "—");
    if (admin) {
      const sel = h("select", { class: "input", "aria-label": `Owner of ${slot.name}` },
        h("option", { value: "" }, "— nobody —"),
        users.map((u) => h("option", { value: u.id, selected: u.id === slot.user_id }, u.username)));
      sel.onchange = async () => {
        try {
          const result = await post(`/api/games/${id}/slots/${slot.slot}/assign`, { user_id: sel.value ? Number(sel.value) : null });
          applyGame(result);
          if (result.configs_added) toast(`Copied ${result.configs_added} config file(s) from this game into the user's library`, "success");
        } catch (err) { toast(err.message, "error"); applyGame(await get(`/api/games/${id}`)); }
      };
      return sel;
    }
    if (slot.mine) {
      return h("span", { class: "row" }, h("strong", {}, "You"),
        h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => applyGame(await post(`/api/games/${id}/slots/${slot.slot}/unclaim`))) }, "Release"));
    }
    if (slot.owner) return slot.owner;
    if (slot.claimable) {
      return h("button", { class: "btn sm primary", type: "button", onclick: action(async () => {
        const result = await post(`/api/games/${id}/slots/${slot.slot}/claim`);
        applyGame(result);
        toast(`You now own ${slot.name}`, "success");
        if (result.configs_added) toast(`Your YAML was added to your configs`, "success");
      }) }, "This is mine");
    }
    return h("span", { class: "muted" }, "unclaimed");
  }

  function drawSlots() {
    const rows = game.slots;
    const myFiles = rows.filter((s) => s.mine).flatMap((s) => s.files).length;
    clear(slotsHost).append(h("div", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, "Slots"),
        myFiles > 1 ? h("a", { class: "btn sm", href: `/api/games/${id}/my-files.zip` }, icon("download", 14), "Download all my files") : null),
      h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["#", "Player", "Game", "Owner", "Progress", "Status", "Files"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.map((s) => h("tr", {},
          h("td", { class: "muted" }, s.slot),
          h("td", {}, h("span", { class: "slot-name" }, s.name), s.mine ? [" ", badge("you", "accent")] : null),
          h("td", {}, s.game),
          h("td", {}, ownerCell(s)),
          h("td", {}, s.type === 1 ? progressBar(s.checked, s.locations, { compact: true }) : h("span", { class: "muted" }, "—")),
          h("td", {}, statusCell(s)),
          h("td", {}, s.files.length
            ? h("div", { class: "stack tight" }, s.files.map((f) => h("a", { class: "btn sm", href: `/api/games/${id}/files/${f.id}`, title: f.filename },
                icon("download", 14), shortName(f.filename), h("span", { class: "muted" }, fmtBytes(f.size)))))
            : h("span", { class: "muted" }, s.type === 1 ? (admin || s.mine ? "no file" : "") : ""))))))),
      !admin && game.claimable && !game.my_slots.length
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, "Find your slot above and press “This is mine” to get your files.",
          game.single_claim ? " Each player can claim one slot." : "") : null,
      !admin && game.claimable && game.single_claim && game.my_slots.length && game.slots.some((s) => s.type === 1 && !s.user_id)
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, "Each player can claim one slot. To swap, release yours first.") : null));
  }

  function signature() {
    return JSON.stringify([game.state, game.error, game.slots, game.progress, game.password, game.address, game.missing_games, game.my_slots]);
  }
  function drawLive() {
    drawnSignature = signature();
    drawHeader();
    drawInfo();
    drawSlots();
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
    const password = h("input", { class: "input", value: game.password ?? "", placeholder: "none", autocomplete: "off" });
    const adminPassword = h("input", { class: "input", value: game.server_password ?? "", placeholder: "none (no remote admin)", autocomplete: "off" });
    const visibility = select([["participants", "Only players in the game"], ["all", "Every user on this server"]], game.visibility);
    const singleClaim = checkbox("Players may claim only one slot each", game.single_claim,
      "Administrators can still assign more slots to someone by hand.");
    const claimable = checkbox("Let users claim unowned slots themselves", game.claimable,
      "Handy when you upload a seed and your friends should pick their own slot.");
    const notes = h("textarea", { class: "input", rows: 2, maxLength: 2000, placeholder: "Only visible to administrators" }, game.notes ?? "");
    const rules = serverOptionsForm(opts);
    const save = h("button", { class: "btn primary", type: "button" }, "Save settings");
    save.onclick = action(async () => {
      const options = rules.read();
      const body = { name: name.value, password: password.value, server_password: adminPassword.value, options,
        visibility: visibility.value, claimable: claimable.input.checked, single_claim: singleClaim.input.checked,
        notes: notes.value };
      if (port.value) body.port = Number(port.value);
      const result = await patch(`/api/games/${id}`, body);
      game = result;
      drawLive();
      toast(result.restart_required ? "Saved. Restart the server to apply the port or startup options." : "Settings saved", "success");
    });
    return h("details", { class: "card" }, h("summary", {}, "Settings"),
      h("div", { class: "stack" },
        h("div", { class: "form-grid" },
          field("Name", name), field("Port", port, "Must be mapped in Docker. Takes effect after a restart."),
          field("Room password", password, "Players need it to connect"),
          field("Remote admin password", adminPassword, "Lets clients use !admin; leave empty to disable"),
          field("Who can see this game", visibility)),
        claimable, singleClaim, field("Notes", notes),
        h("h3", {}, "Server rules"), rules.node,
        h("div", { class: "row" }, save)));
  }

  function filesSection() {
    const all = [...game.slots.flatMap((s) => s.files), ...game.shared_files];
    const slotChoices = [["", "— none —"], ...game.slots.filter((s) => s.type === 1).map((s) => [s.slot, `#${s.slot} ${s.name}`])];
    const input = h("input", { type: "file", class: "input" });
    const slotSel = select(slotChoices, "");
    const pub = checkbox("Visible to every player in the game", false);
    const upload = h("button", { class: "btn", type: "button" }, icon("upload", 14), "Add file");
    upload.onclick = action(async () => {
      if (!input.files[0]) throw new Error("Choose a file first");
      const form = new FormData();
      form.append("file", input.files[0]);
      if (slotSel.value) form.append("slot", slotSel.value);
      form.append("public", pub.input.checked ? "true" : "false");
      game = await api("POST", `/api/games/${id}/files`, form);
      drawLive();
      redrawFiles();
      toast("File added", "success");
    });
    const host = h("div", { class: "stack" });
    const kindBadge = { patch: "accent", spoiler: "warn", yaml: "muted", other: "muted" };
    const redrawFiles = () => {
      const files = [...game.slots.flatMap((s) => s.files), ...game.shared_files];
      clear(host).append(
        files.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["File", "Type", "Assigned to", "Players can see", "", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, files.map((f) => {
            const sel = select(slotChoices, f.slot ?? "");
            sel.onchange = action2(async () => { game = await patch(`/api/games/${id}/files/${f.id}`, { slot: sel.value === "" ? null : Number(sel.value) }); drawLive(); redrawFiles(); });
            const pubBox = h("input", { type: "checkbox", checked: f.public, "aria-label": "Visible to everyone",
              onchange: action2(async () => { game = await patch(`/api/games/${id}/files/${f.id}`, { public: pubBox.checked }); drawLive(); redrawFiles(); }) });
            return h("tr", {},
              h("td", {}, h("span", { class: "mono" }, f.filename), h("div", { class: "muted small" }, fmtBytes(f.size))),
              h("td", {}, badge(f.kind, kindBadge[f.kind])), h("td", {}, sel),
              h("td", {}, f.kind === "patch" ? h("span", { class: "muted small" }, "its owner") : null, pubBox),
              h("td", {}, h("a", { class: "btn sm ghost icon-only", href: `/api/games/${id}/files/${f.id}`, "aria-label": "Download" }, icon("download", 14))),
              h("td", {}, h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": "Delete",
                onclick: action(async () => { if (await confirmDialog(`Delete ${f.filename}?`, { danger: true, confirmText: "Delete" })) { game = await del(`/api/games/${id}/files/${f.id}`); drawLive(); redrawFiles(); } }) }, icon("trash", 14))));
          })))) : h("p", { class: "muted" }, "No files."));
    };
    redrawFiles();
    return h("details", { class: "card" }, h("summary", {}, `Files (${all.length})`),
      h("div", { class: "stack" },
        h("p", { class: "muted small" }, "Patch files go to the owner of the slot they are assigned to. Spoiler logs and YAMLs stay hidden unless you share them."),
        host, h("hr"),
        h("div", { class: "form-grid" }, field("Add a file", input), field("Assign to slot", slotSel)), pub,
        h("div", { class: "row" }, upload,
          h("a", { class: "btn", href: `/api/games/${id}/multidata` }, icon("download", 14), "Download multiworld data"))));
  }

  function consoleSection() {
    const out = h("div", { class: "console", tabIndex: 0, "aria-label": "Server log" });
    const input = h("input", { class: "input mono", placeholder: "Server command, e.g. /send Alice Hammer or /help", autocomplete: "off" });
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
    const details = h("details", { class: "card" }, h("summary", {}, "Console"),
      h("div", { class: "stack" }, out,
        h("div", { class: "row" }, h("div", { class: "grow" }, input), h("button", { class: "btn", type: "button", onclick: send }, "Send")),
        h("p", { class: "muted small" }, "Commands run on the server console. Type /help to list them.")));
    details.addEventListener("toggle", () => {
      clearInterval(timer);
      if (details.open) { poll(); timer = setInterval(() => { if (!document.hidden) poll(); }, 1500); }
    });
    cleanup(() => clearInterval(timer));
    return details;
  }

  function dangerSection() {
    return h("details", { class: "card" }, h("summary", {}, "Danger zone"),
      h("div", { class: "stack" },
        h("div", { class: "row spread" },
          h("div", {}, h("strong", {}, "Reset progress"), h("div", { class: "muted small" }, "Deletes the server's save so the game starts from zero. The server must be stopped.")),
          h("button", { class: "btn danger", type: "button", disabled: isActive(game) || !game.has_save, onclick: action(async () => {
            if (!(await confirmDialog("All progress, received items and hints of this game will be lost.", { title: "Reset progress?", danger: true, confirmText: "Reset" }))) return;
            applyGame(await post(`/api/games/${id}/reset-save`));
            toast("Progress reset", "success");
          }) }, "Reset progress")),
        h("div", { class: "row spread" },
          h("div", {}, h("strong", {}, "Delete this game"), h("div", { class: "muted small" }, "Removes the multiworld, its save and all files for every player.")),
          h("button", { class: "btn danger", type: "button", onclick: action(async () => {
            if (!(await confirmDialog(`“${game.name}” and everything stored for it will be deleted.`, { title: "Delete game?", danger: true, confirmText: "Delete game" }))) return;
            await del(`/api/games/${id}`);
            toast("Game deleted", "success");
            location.hash = "#/games";
          }) }, icon("trash", 14), "Delete game"))));
  }

  // -------------------------------------------------------------------------------------------------------------------
  root.append(headerHost, infoHost, slotsHost, adminHost);
  drawLive();
  if (admin) {
    adminHost.append(settingsSection(), filesSection(), consoleSection(), dangerSection());
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
