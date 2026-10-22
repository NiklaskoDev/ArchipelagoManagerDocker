import { h, clear, icon, toast, action, confirmDialog, fmtAgo, fmtBytes } from "../util.js";
import { api, get, del, isAdmin } from "../api.js";
import { pageHeader, empty, chips, badge, missingGamesPanel, missingRomsPanel, promptMissingGames, checkbox } from "../components.js";
import { t, tServer, plural } from "../i18n.js";

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
  let showAll = false;
  const missingHost = h("div");
  const listHost = h("div");

  async function load() {
    const [configs, missing] = await Promise.all([get(`/api/configs${showAll ? "?all=true" : ""}`), get("/api/missing-games")]);
    if (!isCurrent()) return;
    clear(missingHost).append(missingGamesPanel(missing, load) || "", missingRomsPanel(configs.map((c) => c.missing_roms)));
    draw(configs);
  }

  function draw(configs) {
    clear(listHost);
    if (!configs.length) {
      listHost.append(dropArea(
        h("h3", {}, t("No configs yet")),
        h("p", { class: "muted" }, t("A config (YAML file) tells the randomizer which game you play and how it should be set up. Drop files here, or:")),
        h("div", { class: "row center" },
          h("button", { class: "btn primary", type: "button", onclick: pickFiles }, icon("upload", 14), t("Upload YAML files")),
          h("a", { class: "btn", href: "#/configs/new" }, icon("file", 14), t("Create from template")))));
      return;
    }
    listHost.append(dropArea(null), h("div", { class: "card flush table-wrap", style: { marginTop: "14px" } }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [showAll ? t("Owner") : null, t("File"), t("Player name"), t("Games"), t("Updated"), ""]
        .filter((text) => text !== null).map((text) => h("th", {}, text)))),
      h("tbody", {}, configs.map((c) => h("tr", {},
        showAll ? h("td", {}, c.owner) : null,
        h("td", {}, h("a", { href: `#/configs/${c.id}`, class: "slot-name" }, c.name),
          c.source_game_id ? [" ", h("a", { href: `#/games/${c.source_game_id}` }, badge(t("from a game"), "accent"))] : null,
          h("div", { class: "muted small" }, fmtBytes(c.size))),
        h("td", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, t("from file name"))),
        h("td", {}, chips(c.games, c.missing_games)),
        h("td", { class: "muted" }, fmtAgo(c.updated_at)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          h("a", { class: "btn sm", href: `#/configs/${c.id}` }, icon("edit", 14), t("Edit")),
          h("a", { class: "btn sm ghost icon-only", href: `/api/configs/${c.id}/download`, "aria-label": t("Download"), title: t("Download") }, icon("download", 14)),
          h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": t("Delete"), title: t("Delete"), onclick: action(async () => {
            if (!(await confirmDialog(t("Delete {name}?", { name: c.name }), { danger: true, confirmText: t("Delete") }))) return;
            await del(`/api/configs/${c.id}`);
            toast(t("Config deleted"), "success");
            await load();
          }) }, icon("trash", 14))))))))));
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
      h("a", { class: "btn primary", href: "#/configs/new" }, icon("plus", 14), t("New from template"))),
    fileInput, missingHost, listHost);
  await load();
}
