import { h, clear, icon, openModal, toast, action, fmtDate, fmtSpan } from "../util.js";
import { api, get, isAdmin, isOrganizer } from "../api.js";
import { pageHeader, empty, stateBadge, chips, badge, controlButtons, copyField, checkbox, field, promptMissingGames, apVersionField } from "../components.js";
import { t, tServer } from "../i18n.js";

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
  const organizer = isOrganizer();  // adds games
  const host = h("div");
  let signature = "";

  async function load(force = false) {
    const games = await get("/api/games");
    if (!isCurrent()) return;
    const sig = JSON.stringify(games);
    if (!force && sig === signature) return;
    signature = sig;
    draw(games);
  }

  function draw(games) {
    clear(host);
    if (!games.length) {
      host.append(empty(t("No games here yet"),
        organizer ? t("Upload the zip of a pregenerated multiworld, or generate one from player configs.")
              : t("You will see a game here once an administrator adds you to one."),
        organizer ? h("button", { class: "btn primary", type: "button", onclick: action(uploadDialog) }, icon("upload", 14), t("Add a game")) : null,
        organizer ? h("a", { class: "btn", href: "#/generate" }, icon("zap", 14), t("Generate")) : null));
      return;
    }
    const running = games.filter((g) => !g.finished_at);
    const finished = games.filter((g) => g.finished_at).sort((a, b) => b.finished_at - a.finished_at);
    if (running.length) host.append(activeTable(running));
    else host.append(h("p", { class: "muted" }, t("No game is being played right now.")));
    if (finished.length) {
      host.append(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Archive"))),
        h("p", { class: "muted small" }, t("Games where every player reached their goal, or that an administrator moved here.")),
        archiveTable(finished)));
    }
  }

  function activeTable(games) {
    return h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("Game"), t("Status"), t("Server"), t("Players"), t("Worlds"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, games.map((g) => h("tr", {},
        h("td", {}, h("a", { href: `#/games/${g.id}`, class: "slot-name" }, g.name),
          g.race_mode ? [" ", badge(t("Race"), "warn")] : null,
          g.offered_slots.length ? [" ", badge(t("Slot offered"), "accent")] : null,
          g.my_slots.length ? h("div", { class: "muted small" }, t("You: {names}", { names: g.my_slots.map((s) => s.name).join(", ") })) : null),
        h("td", {}, stateBadge(g.state)),
        h("td", {}, g.address ? copyField(g.address) : h("span", { class: "muted" }, t("no port"))),
        h("td", {}, `${g.claimed_count} / ${g.player_count}`, h("div", { class: "muted small" }, t("with an owner"))),
        h("td", {}, chips(g.games, g.missing_games)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          controlButtons(g, () => load(true)), h("a", { class: "btn sm", href: `#/games/${g.id}` }, t("Open")))))))));
  }

  function archiveTable(games) {
    return h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("Game"), t("Finished"), t("Time played"), t("Goals reached"), t("Worlds"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, games.map((g) => h("tr", {},
        h("td", {}, h("a", { href: `#/games/${g.id}`, class: "slot-name" }, g.name),
          g.my_slots.length ? h("div", { class: "muted small" }, t("You: {names}", { names: g.my_slots.map((s) => s.name).join(", ") })) : null),
        h("td", {}, fmtDate(g.finished_at)),
        h("td", {}, g.play_started_at ? fmtSpan(g.finished_at - g.play_started_at) : h("span", { class: "muted" }, "—")),
        h("td", {}, `${g.goal_count} / ${g.player_count}`),
        h("td", {}, chips(g.games, g.missing_games)),
        h("td", { class: "actions" }, h("a", { class: "btn sm", href: `#/games/${g.id}` }, t("Results"))))))));
  }

  async function uploadDialog() {
    const version = apVersionField(await get("/api/ap-versions"));
    const input = h("input", { type: "file", accept: ".zip,.archipelago", class: "input" });
    const name = h("input", { class: "input", placeholder: t("Defaults to the file name") });
    const start = checkbox(t("Start the server right away"), true);
    const note = h("div", { class: "muted small" });
    const drop = h("div", { class: "dropzone", tabIndex: 0 }, t("Drop the generator's .zip (or a .archipelago file) here, or click to choose"));
    const setFile = (file) => {
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      drop.textContent = `${file.name}`;
    };
    drop.onclick = () => input.click();
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); setFile(e.dataTransfer.files[0]); };
    input.onchange = () => setFile(input.files[0]);
    input.hidden = true;
    const submit = h("button", { class: "btn primary", type: "button" }, icon("upload", 14), t("Upload and add"));
    submit.onclick = action(async () => {
      if (!input.files[0]) throw new Error(t("Choose a file first"));
      const form = new FormData();
      form.append("file", input.files[0]);
      form.append("name", name.value);
      form.append("start", start.input.checked ? "true" : "false");
      if (version) form.append("ap_version", version.read());
      note.textContent = t("Uploading and reading the multiworld… large files can take a moment.");
      try {
        const game = await api("POST", "/api/games", form);
        modal.close();
        toast(t("Added \"{name}\"", { name: game.name }), "success");
        if (game.start_error) toast(t("Could not start the server: {error}", { error: tServer(game.start_error) }), "error");
        location.hash = `#/games/${game.id}`;
        promptMissingGames(game.missing_games.map((g) => ({ game: g, games: [{ id: game.id, name: game.name }] })));
      } finally {
        note.textContent = "";
      }
    });
    const modal = openModal({
      title: t("Add a pregenerated game"),
      body: h("div", { class: "stack" }, drop, input,
        h("p", { class: "muted small" }, t("Files for each player are extracted and linked to users whose YAML player name matches the slot name. You can also assign slots by hand afterwards.")),
        h("p", { class: "muted small" }, t("Moving a game from archipelago.gg? Add the zip you uploaded there; its progress can then be taken over on the game page.")),
        field(t("Name"), name), version?.node, start, note),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), submit],
    });
  }

  root.append(
    pageHeader(t("Games"), admin ? t("Every hosted multiworld on this server.")
      : organizer ? t("Multiworlds you run or are part of.") : t("Multiworlds you are part of."),
      organizer ? h("button", { class: "btn primary", type: "button", onclick: action(uploadDialog) }, icon("upload", 14), t("Add a game")) : null,
      organizer ? h("a", { class: "btn", href: "#/generate" }, icon("zap", 14), t("Generate")) : null),
    host);
  await load(true);
  const timer = setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 5000);
  cleanup(() => clearInterval(timer));
}
