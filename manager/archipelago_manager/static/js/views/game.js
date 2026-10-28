import { h, clear, icon, toast, action, confirmDialog, openModal, fmtBytes, fmtAgo, fmtDate, fmtUntil, fmtSpan, shortName,
         toLocalInput, fromLocalInput, withNodes } from "../util.js";
import { api, get, post, patch, del, isAdmin, canGenerate, state } from "../api.js";
import { stateBadge, badge, chips, progressBar, copyField, copyButton, controlButtons, banner, field, checkbox, select,
         missingGamesPanel, isActive, serverOptionsForm, deliveryStatus } from "../components.js";
import { t, tServer, plural, lang } from "../i18n.js";
import { textClientSection } from "../textclient.js";
import { activitySection } from "../feed.js";
import { awaySummary } from "../away.js";
import { progressSection, playedText } from "../progresschart.js";

// what a backup was made for, and the kind of a game file, as the server names them
const BACKUP_REASONS = { auto: t("automatic"), manual: t("made by hand"), "before-reset": t("before a reset"), "before-restore": t("before a restore"),
  "before-import": t("before taking over from archipelago.gg") };
const FILE_KINDS = { patch: t("patch"), spoiler: t("spoiler"), yaml: t("yaml"), other: t("other") };
// what a player says about how they are doing (PUT /api/games/{id}/slots/{slot}/status)
const SLOT_STATUSES = { playing: ["ok", () => t("Playing")], stuck: ["warn", () => t("Stuck")], away: ["muted", () => t("On a break")] };
const STATUS_NOTE_LENGTH = 140;

export default async function render({ root, params, cleanup, isCurrent, query, setTitle, setUnread }) {
  const id = Number(params[0]);
  const admin = isAdmin();
  let game = await get(`/api/games/${id}`);
  const manage = game.can_manage;  // an administrator, or the organizer who added or generated the game
  const users = manage ? await get("/api/users/names") : [];
  const apVersions = manage ? await get("/api/ap-versions") : null;

  const headerHost = h("div");
  // what happened while the player was away, for players of the game
  const awayHost = game.my_slots.length ? awaySummary({ gameId: id, cleanup, isCurrent }) : h("div");
  const infoHost = h("div");
  const trackerHost = h("div");
  const textClientHost = h("div");
  const activityHost = h("div");
  const progressHost = h("div");
  const archiveHost = h("div");
  const slotsHost = h("div");
  const adminHost = h("div", { class: "stack section" });
  let drawnSignature = "";

  // -------------------------------------------------------------------------------------------------------------------
  // header
  // -------------------------------------------------------------------------------------------------------------------
  function drawHeader() {
    setTitle(game.name);
    clear(headerHost).append(
      h("div", { class: "muted small" }, h("a", { href: "#/games" }, t("← All games"))),
      h("div", { class: "page-head", style: { marginTop: "8px" } },
        h("div", {},
          h("div", { class: "row" }, h("h1", {}, game.name), stateBadge(game.state), game.race_mode ? badge(t("Race mode"), "warn") : null,
            game.finished_at ? badge(t("Finished"), "ok") : null),
          h("p", { class: "muted" }, [
            game.seed_name ? t("Seed {name}", { name: game.seed_name }) : null,
            game.source === "generated" ? t("generated here") : t("uploaded"),
            t("added {date}", { date: fmtDate(game.created_at) }),
          ].filter(Boolean).join(" · "))),
        h("div", { class: "page-actions" }, controlButtons(game, applyGame),
          // the same configs and options once more, with a new seed
          game.job_available && canGenerate()
            ? h("a", { class: "btn sm", href: `#/generate?from=${game.job_id}` }, icon("restart", 14), t("Generate again")) : null)),
      startBanner(),
      waitingBanner(),
      offersBanner(),
      game.state === "crashed" ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, t("The server stopped unexpectedly.")), " ", game.error ? tServer(game.error) : t("See the console for details."))) : null,
      game.ap_version_missing ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, t("Archipelago {version} is not installed on this server.", { version: game.ap_version })), " ",
        admin ? t("This game runs on it. Install it again under Admin → Settings, or choose another version in the game's settings.")
          : t("This game runs on it, so it cannot start until an administrator installs it again."))) : null,
      game.missing_games.length ? missingGamesPanel(game.missing_games.map((g) => ({ game: g, configs: [], games: [{ id, name: game.name }], pending: [] })),
        refresh) : null);
  }

  // A planned start: when, when the server opens, and a clock counting down to it
  let startTimer = null;
  cleanup(() => clearInterval(startTimer));
  function startBanner() {
    clearInterval(startTimer);
    const start = game.start;
    if (!start || start.stage === "started") return null;
    const left = h("strong");
    const tick = () => {
      const s = Math.round(start.at - Date.now() / 1000);
      left.textContent = s <= 0 ? t("now") : s < 60 ? t("in {n} s", { n: s }) : fmtUntil(start.at);
    };
    tick();
    startTimer = setInterval(tick, 1000);
    const opened = start.stage === "opened";
    const how = [
      opened ? t("The server is open: connect now and get ready.")
        : start.open_minutes ? t("The server opens at {time}, so everyone can connect and get ready first.", { time: fmtDate(start.opens_at) })
          : t("The server opens at the start."),
      start.countdown ? t("A countdown in every client ends at the start.") : null,
    ].filter(Boolean).join(" ");
    return banner(opened ? "ok" : "info", icon("calendar", 18), h("div", { class: "grow" },
      h("div", {}, t("Starts {date}", { date: fmtDate(start.at) }), " · ", left), h("div", { class: "muted small" }, how)),
      h("a", { class: "btn sm", href: `/api/games/${id}/start.ics?lang=${lang}`, title: t("A calendar file, with a reminder when the server opens") },
        icon("calendar", 14), t("Add to calendar")));
  }

  // The player was reminded that the others play on without them (reminders.py)
  function waitingBanner() {
    return game.my_slots.filter((s) => s.idle_since).map((mine) => {
      const slot = game.slots.find((s) => s.slot === mine.slot);
      return banner("warn", icon("bell", 18), h("div", { class: "grow" },
        h("div", {}, t("You have not played {slot} for {time}, and the others play on.", { slot: mine.name, time: fmtSpan(Date.now() / 1000 - mine.idle_since) })),
        h("div", { class: "muted small" }, t("Say how you are doing, so the others know. No time any more? Hand the slot over and someone plays on from where you are."))),
      slot ? h("div", { class: "row" },
        h("button", { class: "btn sm", type: "button", onclick: () => statusDialog(slot) }, t("Set status")),
        slot.handover ? null : h("button", { class: "btn sm", type: "button", onclick: () => handoverDialog(slot) }, t("Hand over"))) : null);
    });
  }

  // Slots their players hand over to this user, or to whoever takes them
  function offersBanner() {
    if (!game.offered_slots.length) return null;
    return h("div", { class: "stack tight page-banner" }, game.offered_slots.map((o) => banner("info", icon("users", 18),
      h("div", { class: "grow" },
        h("div", {}, o.to_me ? t("{owner} wants to hand {slot} ({game}) over to you.", { owner: o.from, slot: o.name, game: o.game })
          : t("{owner} is looking for someone to take over {slot} ({game}).", { owner: o.from, slot: o.name, game: o.game })),
        h("div", { class: "muted small" }, o.note ? `“${o.note}” · ` : "", t("You get its files and continue where they left off."))),
      h("div", { class: "row" }, acceptButton(o, "primary"), o.to_me ? declineButton(o) : null))));
  }

  function acceptButton(slot, kind = "") {
    return h("button", { class: `btn sm ${kind}`, type: "button", onclick: action(async () => {
      if (!(await confirmDialog(t("You become the player of {slot}: its files, its YAML and everything found so far are yours, and {owner} no longer plays it.",
        { slot: slot.name, owner: slot.handover?.from ?? slot.from }), { title: t("Take over {slot}?", { slot: slot.name }), confirmText: t("Take over") }))) return;
      const result = await post(`/api/games/${id}/slots/${slot.slot}/handover/accept`);
      applyGame(result);
      toast(t("You now own {slot}", { slot: slot.name }), "success");
      if (result.configs_added) toast(t("Your YAML was added to your configs"), "success");
    }) }, t("Take over"));
  }

  function declineButton(slot) {
    return h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => {
      const result = await del(`/api/games/${id}/slots/${slot.slot}/handover`);
      toast(t("You said no to {slot}", { slot: slot.name }), "success");
      if (result.declined) location.hash = "#/games";  // the game was only visible for the offer
      else applyGame(result);
    }) }, t("No, thanks"));
  }

  // The player hands their slot over: to a user they name, or to whoever takes it. It stays theirs until then.
  function handoverDialog(slot) {
    let anyone = true;
    const name = `handover-${slot.slot}`;
    const user = h("input", { class: "input", autocomplete: "off", disabled: true, placeholder: t("Their user name on this server") });
    const choices = [[true, t("Whoever takes it"), game.visibility === "all" ? t("Everyone on this server can see this game and take the slot.")
      : t("Everyone in this game can take the slot.")], [false, t("A particular user"), t("Only they can take it; they see this game until they do or say no.")]];
    const radios = choices.map(([value, label, hint]) => {
      const input = h("input", { type: "radio", name, checked: value === anyone });
      input.onchange = () => { anyone = value; user.disabled = anyone; if (!anyone) user.focus(); };
      return h("label", { class: "check" }, input, h("span", {}, label, h("span", { class: "hint" }, hint)));
    });
    const note = h("input", { class: "input", maxlength: STATUS_NOTE_LENGTH, placeholder: t("For example: I have no time any more, the next item is in the Water Temple") });
    const save = h("button", { class: "btn primary", type: "button" }, t("Hand over"));
    const modal = openModal({
      title: t("Hand over {slot}", { slot: slot.name }),
      body: h("div", { class: "stack" },
        h("p", { class: "muted small" }, t("Whoever takes the slot over gets its files and its YAML, and plays on from where you are. Until then it stays yours, and you can take the offer back.")),
        h("div", { class: "stack tight" }, radios), field(t("User"), user), field(t("Note"), note)),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save],
    });
    save.onclick = action(async () => {
      if (!anyone && !user.value.trim()) throw new Error(t("Enter the user name of whoever should take it over"));
      applyGame(await api("PUT", `/api/games/${id}/slots/${slot.slot}/handover`, { to: anyone ? null : user.value.trim(), note: note.value }));
      modal.close();
      toast(t("{slot} is offered", { slot: slot.name }), "success");
    });
  }

  // Who a slot is handed over to, and what the viewer can do about it
  function handoverLine(slot) {
    const o = slot.handover;
    if (!o) return null;
    const mine = slot.mine || manage;
    return h("div", { class: "stack tight" },
      h("span", { class: "small" }, badge(t("Handing over"), "warn"), " ",
        o.to ? (o.to_me ? t("to you") : t("to {user}", { user: o.to })) : t("to whoever takes it")),
      o.note ? h("span", { class: "muted small" }, o.note) : null,
      h("div", { class: "row" },
        o.can_accept && !slot.mine ? acceptButton(slot, "primary") : null,
        o.to_me ? declineButton(slot) : null,
        mine ? h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => {
          applyGame(await del(`/api/games/${id}/slots/${slot.slot}/handover`));
          toast(t("The offer is taken back"), "success");
        }) }, t("Take back")) : null));
  }

  // A game that falls asleep: a player's visit wakes it up, and keeps it from falling asleep for a while
  const wakes = () => game.sleep_minutes && game.my_slots.length && !game.finished_at && ["sleeping", "running"].includes(game.state);
  async function wake() {
    const next = await post(`/api/games/${id}/wake`);
    if (isCurrent()) applyGame(next);
  }
  function sleepNote() {
    if (game.state === "sleeping") {
      return h("div", { class: "row spread" },
        h("span", { class: "muted small" }, t("It wakes up when one of its players opens this page.")),
        game.my_slots.length && !game.can_control
          ? h("button", { class: "btn sm primary", type: "button", onclick: action(wake) }, icon("play", 14), t("Wake up")) : null);
    }
    if (!game.sleep_minutes || !isActive(game)) return null;
    return h("p", { class: "muted small" }, plural(game.sleep_minutes,
      "Falls asleep after {n} minute with nobody connected. Opening this page wakes it up again.",
      "Falls asleep after {n} minutes with nobody connected. Opening this page wakes it up again."));
  }

  function drawInfo() {
    const running = game.state === "running";
    clear(infoHost).append(h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, t("Connect")),
        game.state === "sleeping" ? h("span", { class: "muted small" }, t("The server is asleep."))
          : !running && game.state !== "starting" ? h("span", { class: "muted small" }, t("The server is not running right now.")) : null),
      sleepNote(),
      h("dl", { class: "kv" },
        h("dt", {}, t("Server")), h("dd", {}, game.address ? h("div", { class: "row" }, copyField(game.address),
          game.tls ? h("span", { class: "badge ok" }, icon("lock", 12), t("encrypted")) : null)
          : h("span", { class: "muted" }, game.state === "sleeping" ? t("Gets a port when it wakes up") : t("No port assigned"))),
        game.password ? [h("dt", {}, t("Password")), h("dd", {}, copyField(game.password))] : null,
        game.my_slots.length ? [h("dt", {}, plural(game.my_slots.length, "Your slot", "Your slots")),
          h("dd", { class: "stack tight" }, game.my_slots.map((s) => h("div", { class: "row" },
            copyField(s.name), h("span", { class: "muted" }, s.game),
            game.address ? h("a", { class: "btn sm", href: launchLink(s), title: t("Opens Archipelago's Launcher on this computer, connected to your slot") },
              icon("external", 14), t("Open in Archipelago")) : null)))] : null,
        h("dt", {}, "Archipelago"), h("dd", {}, game.ap_version || "—",
          game.ap_version_own ? h("span", { class: "muted small" }, " ", t("(use a client of this version)")) : null),
        h("dt", {}, t("Progress")), h("dd", {}, progressBar(game.progress.checked, game.progress.total))),
      h("p", { class: "muted small" }, game.password
        ? t("In your Archipelago client, enter the server address, your slot name and the password.")
        : t("In your Archipelago client, enter the server address and your slot name."),
        game.tls && game.address ? [" ", withNodes(t("The connection is encrypted; a client that does not find that out by itself takes {address}."),
          { address: h("span", { class: "mono" }, `wss://${game.address}`) })] : null,
        game.my_slots.length && game.address ? [" ", t("Or press “{button}”: with Archipelago installed, its Launcher opens the client for your game already connected (the Text Client, if your game's client does not take links).",
          { button: t("Open in Archipelago") })] : null)));
  }

  // Archipelago's Launcher opens archipelago://slot:password@host:port links (Launcher.handle_uri): it offers the clients
  // of the game that take such a link, else the Text Client, and they connect to the slot.
  function launchLink(slot) {
    const password = game.password ? `:${encodeURIComponent(game.password)}` : "";
    return `archipelago://${encodeURIComponent(slot.name)}${password}@${game.address}?game=${encodeURIComponent(slot.game)}`;
  }

  // -------------------------------------------------------------------------------------------------------------------
  // slots
  // -------------------------------------------------------------------------------------------------------------------
  function statusCell(slot) {
    if (slot.type === 2) return h("span", { class: "muted" }, t("Group"));
    if (slot.type !== 1) return h("span", { class: "muted" }, t("Spectator"));
    const kind = { online: "ok", goal: "ok", offline: "muted" }[slot.status];
    const label = { online: t("Online"), goal: t("Goal complete"), offline: slot.last_activity ? t("Seen {ago}", { ago: fmtAgo(slot.last_activity) }) : t("Offline") }[slot.status];
    const said = slot.player_status && slot.status !== "goal" ? SLOT_STATUSES[slot.player_status] : null;
    return h("div", { class: "stack tight" },
      h("span", { class: "status-line" }, h("span", { class: `dot ${kind}` }), label),
      said ? h("div", { class: "slot-said", title: fmtDate(slot.status_at) }, h("span", { class: `badge ${said[0]}` }, said[1]()),
        slot.status_note ? h("span", { class: "small" }, ` ${slot.status_note}`) : null) : null,
      slot.mine && slot.status !== "goal" ? h("button", { class: "btn sm ghost", type: "button", style: { alignSelf: "flex-start" },
        onclick: () => statusDialog(slot) },
        slot.player_status ? t("Change status") : t("Set status")) : null);
  }

  // The player says how they are doing: everyone in the game sees it, and Discord hears when someone is stuck
  function statusDialog(slot) {
    let chosen = slot.player_status || "";
    const options = [["playing", t("Playing"), t("Busy with the game right now.")],
      ["stuck", t("Stuck"), t("Waiting for items from the others (BK). Say what you need.")],
      ["away", t("On a break"), t("Back later; nobody needs to wait for you.")],
      ["", t("No status"), null]];
    const name = `status-${slot.slot}`;
    const radios = options.map(([value, label, hint]) => {
      const input = h("input", { type: "radio", name, value, checked: value === chosen });
      input.onchange = () => { chosen = value; note.disabled = !chosen; };
      return h("label", { class: "check" }, input, h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null));
    });
    const note = h("input", { class: "input", maxlength: STATUS_NOTE_LENGTH, value: slot.status_note || "", disabled: !chosen,
      placeholder: t("For example: need the Hookshot") });
    const save = h("button", { class: "btn primary", type: "button" }, t("Save"));
    const modal = openModal({
      title: t("Your status in {slot}", { slot: slot.name }),
      body: h("div", { class: "stack" }, h("div", { class: "stack tight" }, radios), field(t("Note"), note)),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save],
    });
    save.onclick = action(async () => {
      applyGame(await api("PUT", `/api/games/${id}/slots/${slot.slot}/status`, { status: chosen || null, note: chosen ? note.value : "" }));
      modal.close();
      toast(t("Status saved"), "success");
    });
  }

  function ownerCell(slot) {
    if (slot.type !== 1) return h("span", { class: "muted" }, "—");
    if (manage) {
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
      return h("div", { class: "stack tight" }, sel, handoverLine(slot));
    }
    if (slot.mine) {
      return h("div", { class: "stack tight" }, h("span", { class: "row" }, h("strong", {}, t("You")),
        slot.handover ? null : h("button", { class: "btn sm ghost", type: "button", title: t("Let someone else play on from where you are"),
          onclick: () => handoverDialog(slot) }, t("Hand over")),
        h("button", { class: "btn sm ghost", type: "button", onclick: action(async () => applyGame(await post(`/api/games/${id}/slots/${slot.slot}/unclaim`))) }, t("Release"))),
        handoverLine(slot));
    }
    if (slot.owner) return slot.handover ? h("div", { class: "stack tight" }, h("span", {}, slot.owner), handoverLine(slot)) : slot.owner;
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
            : h("span", { class: "muted" }, s.type === 1 ? (manage || s.mine ? t("no file") : "") : ""))))))),
      !manage && game.claimable && !game.my_slots.length
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, t("Find your slot above and press “{button}” to get your files.", { button: t("This is mine") }),
          game.single_claim ? ` ${t("Each player can claim one slot.")}` : "") : null,
      !manage && game.claimable && game.single_claim && game.my_slots.length && game.slots.some((s) => s.type === 1 && !s.user_id)
        ? h("p", { class: "muted small", style: { marginTop: "8px" } }, t("Each player can claim one slot. To swap, release yours first.")) : null));
  }

  function signature() {
    return JSON.stringify([game.state, game.error, game.slots, game.progress, game.password, game.address, game.tls, game.missing_games, game.my_slots,
      game.finished_at, game.start, game.sleep_minutes, game.offered_slots]);
  }
  function drawLive() {
    drawnSignature = signature();
    drawHeader();
    drawInfo();
    drawSlots();
    syncTracker();
    syncTextClient();
    syncActivity();
    if (game.can_archive) drawArchive();
    loadHistory().catch(() => {});  // progress, a goal or the archive changed
  }

  // -------------------------------------------------------------------------------------------------------------------
  // progress over time, and the results once everyone is done
  // -------------------------------------------------------------------------------------------------------------------
  let historySignature = "";
  const historyStops = [];
  cleanup(() => historyStops.splice(0).forEach((fn) => fn()));
  async function loadHistory() {
    const data = await get(`/api/games/${id}/history`);
    const { now: _now, ...rest } = data;
    const sig = JSON.stringify([rest, game.slots.filter((s) => s.mine).map((s) => s.slot)]);
    if (!isCurrent() || sig === historySignature) return;
    historySignature = sig;
    historyStops.splice(0).forEach((fn) => fn());
    clear(progressHost);
    if (!data.recorded_from) return;  // the server never ran since the manager keeps a history
    const mine = new Set(game.slots.filter((s) => s.mine).map((s) => s.slot));
    progressHost.append(h("div", { class: "section" }, h("div", { class: "card stack" },
      h("div", { class: "card-head" }, h("h2", {}, data.finished_at ? t("Results") : t("Progress over time")),
        h("span", { class: "muted small" }, playedText(data))),
      progressSection(data, { mine, cleanup: (fn) => historyStops.push(fn) }),
      h("p", { class: "muted small", style: { margin: 0 } }, t("Read from the server's save about once a minute while the server runs.")))));
  }
  const historyTimer = setInterval(() => { if (!document.hidden) loadHistory().catch(() => {}); }, 30000);
  cleanup(() => clearInterval(historyTimer));

  // Games finish by themselves when everyone reached their goal. Whoever runs one, and the player who generated it, can
  // move it to the archive by hand: aborted, if not everyone got there.
  function drawArchive() {
    const aborting = !game.finished_at && game.goal_count < game.player_count;
    clear(archiveHost).append(h("div", { class: "card row spread" },
      h("div", {}, h("strong", {}, game.finished_at ? t("In the archive since {date}", { date: fmtDate(game.finished_at) }) : t("Archive")),
        game.outcome ? [" ", game.outcome === "finished" ? badge(t("Finished"), "ok") : badge(t("Aborted"), "muted")] : null,
        h("div", { class: "muted small" }, game.finished_at
          ? t("Games that are over are listed in the archive. Taking a game back out does not change its progress.")
          : t("A game moves to the archive by itself once every player has reached their goal. Move one there by hand when it will not be played to the end."))),
      h("button", { class: "btn", type: "button", onclick: action(async () => {
        if (aborting && !(await confirmDialog(t("Not every player has reached their goal, so it is listed as aborted. The server keeps running until it is stopped; you can take the game back out later."),
          { title: t("Move this game to the archive?"), confirmText: t("Move to the archive") }))) return;
        applyGame(await post(`/api/games/${id}/archive`, { archived: !game.finished_at }));
      }) }, game.finished_at ? t("Back to the running games") : t("Move to the archive"))));
  }

  // The text client, for the player's own slots only: it speaks in their name
  let textClientKey = null, textClientCleanup = () => {};
  cleanup(() => textClientCleanup());
  // the activity feed, rebuilt only when the viewer's own slots change (what "about me" means)
  let activityKey = null;
  const activityStops = [];
  cleanup(() => activityStops.splice(0).forEach((fn) => fn()));
  function syncActivity() {
    const names = game.my_slots.map((s) => s.name);
    const key = names.join("\n");
    if (key === activityKey) return;
    activityKey = key;
    activityStops.splice(0).forEach((fn) => fn());
    setUnread(0);
    clear(activityHost).append(activitySection({ gameId: id, mySlots: names, cleanup: (fn) => activityStops.push(fn), isCurrent,
      onUnread: setUnread }));
  }

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

  // Players get their own slots, whoever runs the game every player slot; rebuilt only when that list changes (a claim,
  // an assignment).
  function syncTracker() {
    const slots = game.slots.filter((s) => s.type === 1 && (manage || s.mine));
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
    const rulesHost = h("div", { class: "row", style: { gap: "8px 16px" } });
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

    const HINT_LABELS = { 0: t("Not found yet"), 10: t("No priority"), 20: t("Avoid"), 30: t("Priority") };
    const HINT_CHOICES = [10, 30, 20];  // what the receiver can mark a hint as, in the order Archipelago's client has
    const HINT_RANK = { 30: 0, 0: 1, 10: 1, 20: 2 };  // the items others need most come first

    // The player marks a hint for one of their items: the player in whose world it is sees that in their client
    function hintPicker(x) {
      const running = game.state === "running";
      const picker = select([...(HINT_CHOICES.includes(x.status) ? [] : [[x.status, HINT_LABELS[x.status] || HINT_LABELS[0]]]),
        ...HINT_CHOICES.map((status) => [status, HINT_LABELS[status]])], x.status);
      Object.assign(picker, { disabled: !running, title: running ? t("Tell the others how much you need this item") : t("Start the server first") });
      picker.style.maxWidth = "180px";
      picker.setAttribute("aria-label", t("Status of the hint for {item}", { item: x.item }));
      picker.onchange = async () => {
        picker.disabled = true;
        try {
          const result = await api("PUT", `/api/games/${id}/slots/${current}/hints/${x.finder_slot}/${x.location_id}`, { status: Number(picker.value) });
          x.status = result.status;
          toast(t("Marked {item} as {status}", { item: x.item, status: HINT_LABELS[x.status] }), "success");
        } catch (err) {
          toast(err.message, "error");
        }
        draw();
      };
      return picker;
    }

    function hintsTab() {
      const canMark = slots.find((s) => s.slot === current)?.mine;
      const status = (x, mine) => {
        if (x.found) return badge(t("Found"), "ok");
        if (mine && canMark && x.location_id != null) return hintPicker(x);
        return x.status === 30 ? badge(HINT_LABELS[30], "warn") : h("span", { class: "muted" }, HINT_LABELS[x.status] || HINT_LABELS[0]);
      };
      const table = (hints, mine) => h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Item"), mine ? t("Is at") : t("For"), t("Status")].map((text) => h("th", {}, text)))),
        h("tbody", {}, hints.map((x) => h("tr", { style: x.found ? { opacity: 0.6 } : undefined },
          h("td", {}, itemLabel(x.item, x.flags)),
          h("td", {}, mine ? [x.location, h("div", { class: "muted small" }, x.finder === data.name ? t("in your own world") : t("in {player}'s world", { player: x.finder }),
            x.entrance ? ` · ${x.entrance}` : "")] : [x.receiver, h("div", { class: "muted small" }, t("at {location}", { location: x.location }), x.entrance ? ` · ${x.entrance}` : "")]),
          h("td", {}, status(x, mine)))))));
      const forMe = data.hints.filter((x) => x.for_me && matches(x.item, x.location, x.finder, x.entrance));
      const inMine = data.hints.filter((x) => !x.for_me && x.in_my_world && matches(x.item, x.location, x.receiver, x.entrance))
        .sort((a, b) => a.found - b.found || (HINT_RANK[a.status] ?? 1) - (HINT_RANK[b.status] ?? 1));
      if (!data.hints.length) return h("p", { class: "muted" }, t("No hints yet. Ask for one in your client with !hint followed by an item name."));
      return h("div", { class: "stack" },
        h("div", { class: "stack tight" }, h("strong", {}, t("Where your items are")),
          canMark && forMe.some((x) => !x.found) ? h("span", { class: "muted small" }, t("Mark the items you need most as priority, and those you can do without as avoid: whoever has them in their world sees it in their client.")) : null,
          forMe.length ? table(forMe, true) : h("span", { class: "muted" }, t("None."))),
        h("div", { class: "stack tight" }, h("strong", {}, t("Other players' items in your world")), inMine.length ? table(inMine, false) : h("span", { class: "muted" }, t("None."))));
    }

    // Hint points, and !release and !collect as the server's rules allow them now (the server has the last word)
    const ACTIONS = {
      release: { label: t("Release my items"), title: t("Release your items?"),
        hint: t("Sends the items still in your world to their players (!release)"),
        confirm: t("Every item still in your world goes to the player it belongs to, as if you had checked all your locations. This cannot be undone."),
        done: t("Released: your remaining items are on their way to their players."),
        after_goal: t("Release: once you reach your goal"), automatic: t("Release: happens by itself when you reach your goal"),
        disabled: t("Release: turned off on this server") },
      collect: { label: t("Collect my items"), title: t("Collect your items?"),
        hint: t("Gets your items that are still in other worlds (!collect)"),
        confirm: t("Every item of yours still in another world comes to you, and the places they were in count as checked for their players. This cannot be undone."),
        done: t("Collected: your remaining items are on their way to you."),
        after_goal: t("Collect: once you reach your goal"), automatic: t("Collect: happens by itself when you reach your goal"),
        disabled: t("Collect: turned off on this server") },
    };
    function drawRules() {
      const points = data.hint_cost
        ? t("{points} hint points · a hint costs {cost}", { points: data.hint_points, cost: data.hint_cost })
        : t("{points} hint points · hints are free", { points: data.hint_points });
      let next = null;
      const affordable = data.hint_cost ? Math.floor(data.hint_points / data.hint_cost) : 0;
      const checksToGo = data.points_per_check ? Math.ceil((data.hint_cost - data.hint_points) / data.points_per_check) : 0;
      if (affordable > 0) {
        next = plural(affordable, "enough for {n} hint", "enough for {n} hints");
      } else if (data.hint_cost && checksToGo > 0) {
        next = plural(checksToGo, "next hint after {n} more check", "next hint after {n} more checks");
      } else if (data.hint_cost) {
        next = t("checks earn no hint points on this server");
      }
      const mine = slots.find((s) => s.slot === current)?.mine;
      const running = game.state === "running";
      const controls = mine ? Object.entries(ACTIONS).map(([key, a]) => {
        const rule = data[key];
        if (rule === "done") return null;
        if (rule !== "available") return h("span", { class: "muted small" }, a[rule]);
        const button = h("button", { class: "btn sm", type: "button", disabled: !running, title: running ? a.hint : t("Start the server first") }, a.label);
        button.onclick = action(async () => {
          if (!(await confirmDialog(a.confirm, { title: a.title, confirmText: a.label, danger: true }))) return;
          const result = await post(`/api/games/${id}/slots/${current}/action`, { action: key });
          toast(result.done ? a.done : tServer(result.message), result.done ? "success" : "error", result.done ? 4500 : 8000);
          if (result.done) setTimeout(load, 2500);  // the server saves right away; the tracker reads the save
        });
        return button;
      }) : [];
      clear(rulesHost).append(h("span", { class: "small" }, points, next ? h("span", { class: "muted" }, ` · ${next}`) : null), ...controls.filter(Boolean));
    }

    function draw() {
      if (!data) return;
      drawRules();
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
        const sig = JSON.stringify(fresh) + game.state;  // the buttons need a running server
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
      h("div", { class: "card-head" }, h("h2", {}, manage && !slots.some((s) => s.mine) ? t("Tracker") : t("Your tracker")), picker),
      summary, rulesHost, h("div", { class: "row spread" }, tabs, h("div", { style: { minWidth: "200px" } }, search)), content,
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
    const notes = h("textarea", { class: "input", rows: 2, maxLength: 2000, placeholder: t("Only visible to whoever runs this game") }, game.notes ?? "");
    const shareSpoiler = checkbox(t("Share the spoiler log with the players when everyone has finished"), game.share_spoiler_when_done,
      t("When every player has reached their goal (or you move the game to the archive), the spoiler log becomes visible to every player in the game."));
    const rules = serverOptionsForm(opts);
    // the Archipelago version the server runs on: the server's own, or one installed besides it
    const versionChoices = [["", t("This server's ({version})", { version: apVersions.default || "?" })],
      ...apVersions.versions.filter((v) => v.status === "installed" || v.version === game.ap_version_own)
        .map((v) => [v.version, v.status === "installed" ? v.version : t("{version} (not installed)", { version: v.version })])];
    if (game.ap_version_own && !versionChoices.some(([value]) => value === game.ap_version_own)) {
      versionChoices.push([game.ap_version_own, t("{version} (not installed)", { version: game.ap_version_own })]);
    }
    const apVersion = select(versionChoices, game.ap_version_own || "");
    // after how many days without playing, while the others play on, a player is reminded (null: the server's default)
    const remindChoices = [["", game.remind_days_default ? plural(game.remind_days_default, "As on this server (after {n} day)", "As on this server (after {n} days)")
      : t("As on this server (never)")], ["0", t("Never")],
      ...[1, 2, 3, 5, 7, 14].map((n) => [String(n), plural(n, "After {n} day", "After {n} days")])];
    if (game.remind_days != null && !remindChoices.some(([value]) => value === String(game.remind_days))) {
      remindChoices.push([String(game.remind_days), plural(game.remind_days, "After {n} day", "After {n} days")]);
    }
    const remind = select(remindChoices, game.remind_days == null ? "" : String(game.remind_days));
    // a planned start; the field holds minutes, so an unchanged value is not sent back
    let plannedValue = toLocalInput(game.start?.at);
    const startAt = h("input", { class: "input", type: "datetime-local", value: plannedValue });
    const openMinutes = h("input", { class: "input", type: "number", min: 0, max: 1440, value: game.start?.open_minutes ?? 10 });
    const countdown = checkbox(t("Count down to the start in every client"), game.start ? game.start.countdown : true,
      t("Archipelago's countdown, so that everyone starts at the same moment, as in a race."));
    // the game's own Discord channel
    const webhook = h("input", { class: "input", type: "url", value: game.discord_webhook_url ?? "", autocomplete: "off",
      placeholder: t("Empty: the main channel") });
    const webhookStatus = h("div");
    const drawWebhookStatus = (status) => clear(webhookStatus).append(deliveryStatus(status) || "");
    drawWebhookStatus(game.notify_status);
    const testWebhook = h("button", { class: "btn", type: "button" }, icon("bell", 14), t("Send a test message"));
    testWebhook.onclick = action(async () => {
      const result = await post(`/api/games/${id}/test-notification`, { discord_webhook_url: webhook.value.trim() || null });
      drawWebhookStatus(result.notify_status);
      toast(t("Test message sent. Check your Discord channel."), "success");
    });
    const save = h("button", { class: "btn primary", type: "button" }, t("Save settings"));
    save.onclick = action(async () => {
      const options = rules.read();
      const body = { name: name.value, password: password.value, server_password: adminPassword.value, options,
        visibility: visibility.value, claimable: claimable.input.checked, single_claim: singleClaim.input.checked,
        notes: notes.value, share_spoiler_when_done: shareSpoiler.input.checked, discord_webhook_url: webhook.value.trim(),
        start_open_minutes: Number(openMinutes.value || 0), start_countdown: countdown.input.checked, ap_version: apVersion.value,
        remind_days: remind.value === "" ? null : Number(remind.value) };
      if (startAt.value !== plannedValue) body.start_at = fromLocalInput(startAt.value);
      if (port.value) body.port = Number(port.value);
      const result = await patch(`/api/games/${id}`, body);
      game = result;
      plannedValue = toLocalInput(game.start?.at);
      drawLive();
      toast(result.restart_required ? t("Saved. Restart the server for the changes to take effect.") : t("Settings saved"), "success");
    });
    return h("details", { class: "card" }, h("summary", {}, t("Settings")),
      h("div", { class: "stack" },
        h("div", { class: "form-grid" },
          field(t("Name"), name), field(t("Port"), port, t("Must be mapped in Docker. Takes effect after a restart.")),
          field(t("Room password"), password, t("Players need it to connect")),
          field(t("Remote admin password"), adminPassword, t("Lets clients use !admin; leave empty to disable")),
          field(t("Who can see this game"), visibility),
          field(t("Archipelago version"), apVersion, t("What the server runs, and what players need a client of. Further versions are installed under Admin → Settings."))),
        claimable, singleClaim, shareSpoiler, field(t("Notes"), notes),
        h("h3", {}, t("Planned start")),
        h("p", { class: "muted small" }, t("The server opens by itself before the start and is announced on Discord; until then only whoever runs the game can start it. The results time every goal from the start, to the second.")),
        h("div", { class: "form-grid" },
          field(t("Start time"), startAt, t("In your own time zone. Empty for no planned start.")),
          field(t("Open the server this many minutes before"), openMinutes)),
        countdown,
        h("h3", {}, t("Discord")),
        field(t("Webhook for this game"), webhook,
          t("Messages about this game (online, crashes, goals, hinted items found) go to this channel instead of the main one. Which kinds are sent is set under Admin → Settings.")),
        webhookStatus, h("div", { class: "row" }, testWebhook),
        field(t("Remind players who stopped playing"), remind,
          t("After this many days without playing, while the others play on, a player is reminded on their dashboard and on Discord, where they can ask to be mentioned. Players who said they are on a break are not reminded.")),
        h("h3", {}, t("Server rules")), rules.node,
        h("div", { class: "row" }, save)));
  }

  // A link that shows the game without signing in (spectate.py), for viewers and as an overlay in a stream
  function spectateSection() {
    const host = h("div", { class: "stack" });
    const draw = () => {
      const link = game.spectate;
      const base = `${state.status?.site_url || location.origin}/watch.html#`;
      const intro = h("p", { class: "muted small" }, t("A link that shows this game without signing in: how far everyone got, the standings and race times. Nothing to connect with, and no user names. Open it for viewers, or add the overlay to OBS as a browser source."));
      if (!link) {
        clear(host).append(intro, h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: action(async () => {
          game = await post(`/api/games/${id}/spectate`);
          draw();
          toast(t("Spectator link made"), "success");
        }) }, icon("link", 14), t("Make a link"))));
        return;
      }
      const activityBox = checkbox(t("Also show what happens: items, hints, joins and goals"), link.activity, t("Never the chat. Hints show where items are, which is a spoiler for viewers who play along."));
      activityBox.input.onchange = action2(async () => {
        game = await patch(`/api/games/${id}`, { spectate_activity: activityBox.input.checked });
        draw();
      });
      clear(host).append(intro,
        field(t("Spectator page"), copyField(base + link.token)),
        field(t("Overlay for OBS"), copyField(`${base}${link.token}/overlay`), t("Transparent, with every player's progress and the race clock. A width of about 400 pixels suits it.")),
        activityBox,
        h("div", { class: "row" },
          h("a", { class: "btn", href: base + link.token, target: "_blank", rel: "noopener" }, icon("external", 14), t("Open")),
          h("button", { class: "btn", type: "button", onclick: action(async () => {
            if (!(await confirmDialog(t("The current link stops working. Everyone who should keep watching needs the new one."), { title: t("Make a new link?"), confirmText: t("New link") }))) return;
            game = await post(`/api/games/${id}/spectate`);
            draw();
            toast(t("New spectator link made"), "success");
          }) }, icon("restart", 14), t("New link")),
          h("button", { class: "btn ghost", type: "button", onclick: action(async () => {
            game = await del(`/api/games/${id}/spectate`);
            draw();
            toast(t("Spectating is turned off"), "success");
          }) }, t("Turn off"))));
    };
    draw();
    return h("details", { class: "card" }, h("summary", {}, t("Spectators")), host);
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

  // Moving a game here from archipelago.gg: its progress comes from the room's tracker (ggimport.py)
  function importSection() {
    const room = h("input", { class: "input", placeholder: "https://archipelago.gg/room/…", autocomplete: "off" });
    const go = h("button", { class: "btn", type: "button" }, icon("download", 14), t("Take over the progress"));
    const note = h("p", { class: "muted small" });
    const sync = () => {
      go.disabled = isActive(game);
      note.textContent = isActive(game) ? t("Stop the server first") : "";
    };
    go.onclick = action(async () => {
      if (!room.value.trim()) throw new Error(t("Paste the address of the room first"));
      if (!(await confirmDialog(t("The progress of this game is replaced by the room's. The current progress is kept as a backup."),
        { title: t("Take over the progress?"), confirmText: t("Take over") }))) return;
      const result = await post(`/api/games/${id}/import-gg`, { room: room.value.trim() });
      applyGame(result);
      const r = result.imported;
      toast(t("Taken over: {checked} locations checked, {received} items received, {hints} hints.", r)
        + (r.patches ? ` ${plural(r.patches, "{n} patch file downloaded.", "{n} patch files downloaded.")}` : ""), "success", 9000);
      room.value = "";
    });
    const details = h("details", { class: "card" }, h("summary", {}, t("Move from archipelago.gg")),
      h("div", { class: "stack" },
        h("p", { class: "muted small" }, t("Continue a game that was hosted on archipelago.gg here. archipelago.gg does not hand out a room's multiworld, so add this game from the zip the generator made (the one uploaded there), then paste the room's address: the locations checked, the items received, the hints and who reached their goal are taken from the room's tracker.")),
        h("p", { class: "muted small" }, t("The room must hold this very multiworld. Hint points already spent and what some games keep in the server's data storage are not carried over. Patch files this game has none of yet are downloaded from the room. Let everyone stop playing there first: the tracker is up to a minute behind.")),
        h("div", { class: "row" }, h("div", { class: "grow" }, room), go), note));
    details.addEventListener("toggle", sync);
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
  root.append(headerHost, awayHost, infoHost, trackerHost, textClientHost, activityHost, progressHost, slotsHost, adminHost);
  drawLive();
  // a player who generated the game (and so may archive it) without running it sees the archive on its own
  if (!manage && game.can_archive) adminHost.append(archiveHost);
  if (manage) {
    adminHost.append(settingsSection(), archiveHost, spectateSection(), filesSection(), consoleSection(), backupsSection(), importSection(),
      dangerSection());
    if (query.get("tab") === "console") adminHost.querySelector("details:nth-of-type(4)")?.setAttribute("open", "");
  }
  const timer = setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 4000);
  cleanup(() => clearInterval(timer));
  if (wakes()) wake().catch((err) => toast(err.message, "error"));
}

// change handlers on plain elements still need error toasts
function action2(fn) {
  return async (event) => {
    try { await fn(event); } catch (err) { toast(err.message, "error"); }
  };
}
