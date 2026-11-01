import { h, clear, icon, openModal, toast, action, setText } from "../util.js";
import { api, get, post, del, isAdmin, isOrganizer } from "../api.js";
import { pageHeader, empty, stateBadge, chips, badge, controlButtons, copyField, checkbox, field, promptMissingGames, apVersionField,
  gamesTabs, canLaunch, launchButton, gameSelection } from "../components.js";
import { t, tServer, plural } from "../i18n.js";
import { every } from "../live.js";

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
  const organizer = isOrganizer();  // adds games
  let running = [], signature = "";
  // The parts of the page are shown and hidden rather than made anew, so the search keeps the focus while the list
  // changes underneath it.
  const emptyHost = h("div");
  const tabsHost = h("div");
  const none = h("p", { class: "muted", hidden: true }, t("No game is being played right now."));
  const listHost = h("div");
  const search = h("input", { class: "input", type: "search", placeholder: t("Search by game, world or player…"), "aria-label": t("Search the games") });
  search.oninput = drawList;
  // several games at once, for whoever runs them
  const selection = gameSelection({
    onDone: () => load(true),
    actions: [
      { label: t("Stop"), icon: "stop", run: (g) => post(`/api/games/${g.id}/stop`),
        confirm: (n) => ({ title: plural(n, "Stop {n} game?", "Stop {n} games?"), confirmText: t("Stop"),
          text: t("Their servers stop. Players can no longer connect until a game is started again.") }),
        done: (n) => plural(n, "{n} game stopped", "{n} games stopped") },
      { label: t("Move to the archive"), icon: "package", run: (g) => post(`/api/games/${g.id}/archive`, { archived: true }),
        confirm: (n) => ({ title: plural(n, "Move {n} game to the archive?", "Move {n} games to the archive?"), confirmText: t("Move to the archive"),
          text: t("Their servers are stopped. A game where not every player reached their goal is listed as aborted, and can be taken back out later.") }),
        done: (n) => plural(n, "{n} game moved to the archive", "{n} games moved to the archive") },
      { label: t("Delete"), icon: "trash", danger: true, run: (g) => del(`/api/games/${g.id}`),
        confirm: (n) => ({ title: plural(n, "Delete {n} game?", "Delete {n} games?"), confirmText: t("Delete"),
          text: t("Each game's multiworld, save and the files of every player are deleted. This cannot be undone.") }),
        done: (n) => plural(n, "{n} game deleted", "{n} games deleted") },
    ],
  });
  const card = h("div", { class: "card flush", hidden: true },
    h("div", { class: "row list-tools" }, h("div", { class: "grow list-search" }, search)), selection.bar, listHost);

  async function load(force = false) {
    const games = await get("/api/games");
    if (!isCurrent()) return;
    const sig = JSON.stringify(games);
    if (!force && sig === signature) return;
    signature = sig;
    draw(games);
  }

  function draw(games) {
    clear(emptyHost).append(games.length ? null : empty(t("No games here yet"),
      organizer ? t("Upload the zip of a pregenerated multiworld, or generate one from player configs.")
        : t("You will see a game here once an administrator adds you to one."),
      organizer ? h("button", { class: "btn primary", type: "button", onclick: action(uploadDialog) }, icon("upload", 14), t("Add a game")) : null,
      organizer ? h("a", { class: "btn", href: "#/generate" }, icon("zap", 14), t("Generate")) : null));
    // games that are over are in the archive (views/archive.js)
    running = games.filter((g) => !g.finished_at);
    clear(tabsHost).append(games.length ? gamesTabs("games", games.length - running.length) : null);
    none.hidden = !games.length || running.length > 0;
    card.hidden = !running.length;
    drawList();
  }

  function drawList() {
    const q = search.value.trim().toLowerCase();
    const list = running.filter((g) => !q || [g.name, ...g.games, ...g.my_slots.map((s) => s.name)].some((text) => text.toLowerCase().includes(q)));
    selection.list(list);
    clear(listHost).append(list.length ? activeTable(list) : h("p", { class: "muted list-none" }, t("Nothing matches.")));
  }

  function activeTable(games) {
    return h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, selection.head(), [t("Game"), t("Status"), t("Server"), t("Players"), t("Worlds"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, games.map((g) => h("tr", {},
        selection.cell(g),
        h("td", {}, h("a", { href: `#/games/${g.id}`, class: "slot-name" }, g.name),
          g.race_mode ? [" ", badge(t("Race"), "warn")] : null,
          g.offered_slots.length ? [" ", badge(t("Slot offered"), "accent")] : null,
          g.my_slots.length ? h("div", { class: "muted small" }, t("You: {names}", { names: g.my_slots.map((s) => s.name).join(", ") })) : null),
        h("td", {}, stateBadge(g.state)),
        h("td", {}, g.address ? copyField(g.address) : h("span", { class: "muted" }, t("no port"))),
        h("td", {}, `${g.claimed_count} / ${g.player_count}`, h("div", { class: "muted small" }, t("with an owner"))),
        h("td", {}, chips(g.games, g.missing_games)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          canLaunch(g) ? g.my_slots.map((s) => launchButton(g, s, { named: g.my_slots.length > 1 })) : null,
          controlButtons(g, () => load(true)), h("a", { class: "btn sm", href: `#/games/${g.id}` }, t("Open")))))))));
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
      setText(note, t("Uploading and reading the multiworld… large files can take a moment."));
      try {
        const game = await api("POST", "/api/games", form);
        modal.close();
        toast(t("Added \"{name}\"", { name: game.name }), "success");
        if (game.start_error) toast(t("Could not start the server: {error}", { error: tServer(game.start_error) }), "error");
        location.hash = `#/games/${game.id}`;
        promptMissingGames(game.missing_games.map((g) => ({ game: g, games: [{ id: game.id, name: game.name }] })));
      } finally {
        setText(note, "");
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
    emptyHost, tabsHost, none, card);
  await load(true);
  every(5000, () => load(), cleanup);
}
