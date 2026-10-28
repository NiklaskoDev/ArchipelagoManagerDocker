import { h, clear, icon, toast, action, confirmDialog, fmtAgo, fmtBytes, openModal } from "../util.js";
import { api, get, post, put, del, isAdmin, state } from "../api.js";
import { pageHeader, empty, chips, badge, missingGamesPanel, missingRomsPanel, promptMissingGames, checkbox, field,
         defaultPlayerName, configCheckDialog } from "../components.js";
import { t, tServer, plural } from "../i18n.js";

export default async function render({ root, query, isCurrent }) {
  const admin = isAdmin();
  let showAll = false;
  const missingHost = h("div");
  const listHost = h("div");
  const sharedHost = h("div", { class: "card stack", id: "shared", style: { marginTop: "18px" } });

  async function load() {
    const [configs, missing, shared] = await Promise.all([get(`/api/configs${showAll ? "?all=true" : ""}`),
      get("/api/missing-games"), get("/api/configs/shared")]);
    if (!isCurrent()) return;
    clear(missingHost).append(missingGamesPanel(missing, load) || "", missingRomsPanel(configs.map((c) => c.missing_roms)));
    draw(configs);
    drawShared(shared);
  }

  const setShared = (c, shared) => action(async () => {
    await put(`/api/configs/${c.id}/shared`, { shared });
    toast(shared ? t("{name} is now shared with the other players", { name: c.name }) : t("{name} is no longer shared", { name: c.name }), "success");
    await load();
  });

  // Configs other players share: look at one, download it, or copy it into your own
  function drawShared(shared) {
    clear(sharedHost).append(h("h2", {}, t("Shared by players")),
      h("p", { class: "muted small" }, t("Configs players offer to everyone. Copy one into your configs to use it or change it; the original stays as it is.")));
    if (!shared.length) {
      sharedHost.append(h("p", { class: "muted" }, t("Nobody shares a config yet. Share one of yours with the Share button next to it.")));
      return;
    }
    sharedHost.append(h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("File"), t("Player name"), t("Games"), t("Shared by"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, shared.map((c) => h("tr", {},
        h("td", {}, h("button", { class: "link slot-name", type: "button", onclick: () => preview(c) }, c.name),
          c.description ? h("div", { class: "muted small" }, c.description) : null),
        h("td", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, t("from file name"))),
        h("td", {}, chips(c.games, c.missing_games)),
        h("td", {}, c.mine ? badge(t("you"), "accent") : c.owner, h("div", { class: "muted small" }, fmtAgo(c.shared_at))),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          c.mine || admin ? h("button", { class: "btn sm", type: "button", onclick: setShared(c, false) }, t("Stop sharing")) : null,
          c.mine ? null : h("button", { class: "btn sm", type: "button", onclick: () => copyDialog(c) }, icon("copy", 14), t("Copy")),
          h("a", { class: "btn sm ghost icon-only", href: `/api/configs/${c.id}/download`, "aria-label": t("Download"), title: t("Download") }, icon("download", 14))))))))));
  }

  async function preview(c) {
    let detail;
    try {
      detail = await get(`/api/configs/shared/${c.id}`);
    } catch (err) {
      toast(err.message, "error");
      await load();
      return;
    }
    const text = h("textarea", { class: "input editor", readOnly: true, rows: 18, wrap: "off", spellcheck: false, "aria-label": "YAML" });
    text.value = detail.content;
    const modal = openModal({
      title: detail.name, wide: true,
      body: h("div", { class: "stack" },
        h("div", { class: "row" }, chips(detail.games, detail.missing_games), h("span", { class: "muted small" },
          t("Shared by {owner}, changed {ago}", { owner: detail.owner, ago: fmtAgo(detail.updated_at) }))),
        detail.description ? h("p", {}, detail.description) : null, text),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close")),
        detail.mine ? null : h("button", { class: "btn primary", type: "button", onclick: () => { modal.close(); copyDialog(detail); } },
          icon("copy", 14), t("Copy to my configs"))],
    });
  }

  function copyDialog(c) {
    const single = c.player_names.length <= 1;
    const name = h("input", { class: "input", maxLength: 16, value: single ? defaultPlayerName() : "", autocomplete: "off" });
    const copy = h("button", { class: "btn primary", type: "button" }, icon("copy", 14), t("Copy"));
    copy.onclick = action(async () => {
      const created = await post(`/api/configs/${c.id}/copy`, { player_name: name.value.trim() || null });
      modal.close();
      toast(t("Copied to your configs"), "success");
      location.hash = `#/configs/${created.id}`;
    });
    const modal = openModal({
      title: t("Copy {name}", { name: c.name }),
      body: h("div", { class: "stack" },
        h("p", { class: "muted" }, t("The copy is yours: changing it does not change the original, and the original changing does not change it.")),
        field(t("Your player name"), name, single
          ? (c.player_names.length ? t("Replaces {name} in the file. Every player in a game needs a different name.", { name: c.player_names[0] })
            : t("Every player in a game needs a different name."))
          : t("This file holds several players. Leave this empty and change their names in the copy."))),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), copy],
    });
    name.focus();
  }

  // The search stays where it is while the table below it changes, so it keeps the focus
  let configs = [];
  const search = h("input", { class: "input", type: "search", placeholder: t("Search by file, player name or game…"),
    "aria-label": t("Search configs") });
  search.oninput = () => drawTable();
  const tableHost = h("div");
  const card = h("div", { class: "card flush", style: { marginTop: "14px" } },
    h("div", { class: "row list-tools" }, h("div", { class: "grow list-search" }, search)), tableHost);

  function draw(list) {
    configs = list;
    clear(listHost);
    if (!configs.length) {
      listHost.append(dropArea(
        h("h3", {}, t("No configs yet")),
        h("p", { class: "muted" }, t("A config (YAML file) tells the randomizer which game you play and how it should be set up. Drop files here, or:")),
        h("div", { class: "row center" },
          h("button", { class: "btn primary", type: "button", onclick: pickFiles }, icon("upload", 14), t("Upload YAML files")),
          h("a", { class: "btn", href: "#/configs/new" }, icon("file", 14), t("Create a config")))));
      return;
    }
    listHost.append(dropArea(null), card);
    drawTable();
  }

  function drawTable() {
    const q = search.value.trim().toLowerCase();
    const shown = configs.filter((c) => !q || [c.name, ...c.player_names, ...c.games, showAll ? c.owner : ""]
      .some((text) => (text || "").toLowerCase().includes(q)));
    clear(tableHost).append(shown.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [showAll ? t("Owner") : null, t("File"), t("Player name"), t("Games"), t("Updated"), ""]
        .filter((text) => text !== null).map((text) => h("th", {}, text)))),
      h("tbody", {}, shown.map((c) => h("tr", {},
        showAll ? h("td", {}, c.owner) : null,
        h("td", {}, h("a", { href: `#/configs/${c.id}`, class: "slot-name" }, c.name),
          c.source_game_id ? [" ", h("a", { href: `#/games/${c.source_game_id}` }, badge(t("from a game"), "accent"))] : null,
          c.shared ? [" ", badge(t("shared"), "ok")] : null,
          h("div", { class: "muted small" }, fmtBytes(c.size))),
        h("td", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, t("from file name"))),
        h("td", {}, chips(c.games, c.missing_games)),
        h("td", { class: "muted" }, fmtAgo(c.updated_at)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          h("button", { class: "btn sm", type: "button", title: t("Find out whether the generator takes this config, without generating"),
            onclick: () => configCheckDialog({ name: c.name, load: async () => (await get(`/api/configs/${c.id}`)).content,
              editHref: `#/configs/${c.id}` }) }, icon("check", 14), t("Check")),
          h("a", { class: "btn sm", href: `#/configs/${c.id}` }, icon("edit", 14), t("Edit")),
          c.user_id === state.user.id || c.shared ? h("button", { class: "btn sm ghost", type: "button", "aria-pressed": String(c.shared),
            title: c.shared ? t("Stop sharing this config") : t("Offer this config to the other players to copy"),
            onclick: setShared(c, !c.shared) }, icon("users", 14), c.shared ? t("Stop sharing") : t("Share")) : null,
          h("a", { class: "btn sm ghost icon-only", href: `/api/configs/${c.id}/download`, "aria-label": t("Download"), title: t("Download") }, icon("download", 14)),
          h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": t("Delete"), title: t("Delete"), onclick: action(async () => {
            if (!(await confirmDialog(t("Delete {name}?", { name: c.name }), { danger: true, confirmText: t("Delete") }))) return;
            await del(`/api/configs/${c.id}`);
            toast(t("Config deleted"), "success");
            await load();
          }) }, icon("trash", 14)))))))))
      : h("p", { class: "muted list-none" }, t("Nothing matches.")));
  }

  const fileInput = h("input", { type: "file", multiple: true, accept: ".yaml,.yml,text/yaml", hidden: true });
  const pickFiles = () => fileInput.click();

  async function upload(files) {
    if (!files.length) return;
    const form = new FormData();
    for (const f of files) form.append("files", f);
    try {
      const result = await api("POST", "/api/configs", form);
      if (result.created.length) toast(plural(result.created.length, "Added {n} config", "Added {n} configs"), "success");
      for (const e of result.errors) toast(`${e.filename}: ${tServer(e.error)}`, "error");
      const games = [...new Set(result.created.flatMap((c) => c.missing_games))];
      promptMissingGames(games.map((game) => ({
        game, configs: result.created.filter((c) => c.missing_games.includes(game)).map((c) => ({ id: c.id, name: c.name })),
      })), load);
    } catch (err) {
      toast(err.message, "error");
    }
    await load();
  }
  fileInput.onchange = () => { upload([...fileInput.files]); fileInput.value = ""; };

  function dropArea(...content) {
    const zone = h("div", { class: "dropzone" }, content.filter(Boolean).length ? content : t("Drop YAML files here to add them"));
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
    zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); upload([...e.dataTransfer.files]); };
    if (!content.filter(Boolean).length) zone.onclick = pickFiles;
    return zone;
  }

  const allToggle = admin ? checkbox(t("Show everyone's configs"), false) : null;
  if (allToggle) allToggle.input.onchange = () => { showAll = allToggle.input.checked; load(); };

  root.append(
    pageHeader(admin ? t("Player configs") : t("My configs"), t("YAML files that describe how each player's game is randomized."),
      allToggle,
      h("button", { class: "btn", type: "button", onclick: pickFiles }, icon("upload", 14), t("Upload YAML")),
      h("a", { class: "btn primary", href: "#/configs/new" }, icon("plus", 14), t("New config"))),
    fileInput, missingHost, listHost, sharedHost);
  await load();
  if (query.get("shared")) sharedHost.scrollIntoView({ block: "start" });
}
