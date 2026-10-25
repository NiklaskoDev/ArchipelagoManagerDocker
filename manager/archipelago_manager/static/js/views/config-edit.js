import { h, clear, icon, toast, action, confirmDialog, openModal, fmtAgo } from "../util.js";
import { get, post, put, del, isAdmin } from "../api.js";
import { pageHeader, chips, banner, field, loading, missingGamesPanel, missingRomsPanel, badge, defaultPlayerName } from "../components.js";
import { t, tServer } from "../i18n.js";
import { loadSchema, optionsForm } from "../options-form.js";

// Which tab the editor opens with; people who prefer the YAML keep getting it
const TAB_KEY = "apm-config-tab";
function preferredTab() {
  try { return localStorage.getItem(TAB_KEY) === "yaml" ? "yaml" : "form"; } catch { return "form"; }
}
function rememberTab(tab) {
  try { localStorage.setItem(TAB_KEY, tab); } catch { /* private mode */ }
}

export default async function render({ root, params, query, cleanup, setDirtyCheck, isCurrent }) {
  const id = params[0] ? Number(params[0]) : null;
  if (id === null && !query.get("game")) return renderPicker(root);

  let config = null;
  let text;
  let name;
  let start = preferredTab();
  const newGame = id === null && query.get("game") !== "__blank__" ? query.get("game") : null;
  if (id !== null) {
    config = await get(`/api/configs/${id}`);
    text = config.content;
    name = config.name;
  } else {
    const game = query.get("game");
    if (game === "__blank__") {
      text = "name: Player\ngame: \n";
      start = "yaml";
    } else if (query.get("yaml")) {
      text = await fetch(`/api/templates/yaml?game=${encodeURIComponent(game)}`, { credentials: "same-origin" }).then((r) => {
        if (!r.ok) throw new Error(t("No template for {game}", { game }));
        return r.text();
      });
      start = "yaml";
    } else {
      text = "";  // the form writes the file
      start = "form";
    }
    // templates ship with a placeholder name; give the user a sensible starting point
    text = text.replace(/^name: .*$/m, `name: ${defaultPlayerName()}`);
    name = `${game.replace(/[^A-Za-z0-9 _-]+/g, "").trim() || "config"}.yaml`;
  }

  const editor = h("textarea", { class: "input editor", spellcheck: false, wrap: "off", "aria-label": "YAML" });
  editor.value = text;
  const nameInput = h("input", { class: "input", value: name, maxLength: 120, "aria-label": t("File name") });
  const infoHost = h("div", { class: "stack" });
  const errorHost = h("div");

  // The form and the YAML edit the same file. The form reads the text when it is opened and writes it (on the server,
  // which keeps what the form does not show) when the YAML tab opens or the file is saved after changes in the form.
  let mode = null;
  let form = null;
  let formBase = null;    // the text the form was read from; null while a new file exists only in the form
  let formDirty = false;  // changed in the form since then
  const formHost = h("div", { class: "stack" });
  const yamlHost = h("div", { class: "stack" },
    newGame && !query.get("yaml") ? h("p", { class: "muted small" }, t("Want every option explained in the file?"), " ",
      h("a", { href: `#/configs/new?game=${encodeURIComponent(newGame)}&yaml=1` }, t("Start from the documented template instead"))) : null,
    editor);
  const formTab = h("button", { class: "tab", type: "button", role: "tab" }, icon("settings", 14), t("Options"));
  const yamlTab = h("button", { class: "tab", type: "button", role: "tab" }, icon("file", 14), t("YAML"));

  let saved = { text, name };
  const dirty = () => editor.value !== saved.text || nameInput.value !== saved.name || formDirty;
  setDirtyCheck(dirty);
  const onBeforeUnload = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onBeforeUnload);
  cleanup(() => window.removeEventListener("beforeunload", onBeforeUnload));

  function setMode(next) {
    mode = next;
    formHost.hidden = next !== "form";
    yamlHost.hidden = next !== "yaml";
    for (const [tab, key] of [[formTab, "form"], [yamlTab, "yaml"]]) {
      tab.setAttribute("aria-selected", String(key === next));
      if (key === next) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    }
  }

  const needsWrite = () => form !== null && (formDirty || formBase === null);

  async function writeForm() {
    const values = form.read();
    const result = await post("/api/configs/form/write", { content: formBase, game: form.game, ...values });
    formBase = result.content;
    formDirty = false;
    return result.content;
  }

  // `opening`: the page is just opening, so a file the form cannot edit simply shows as YAML
  async function showForm({ opening = false } = {}) {
    if (form && (mode === "form" || formBase === editor.value)) { setMode("form"); return; }  // the YAML did not change since
    setMode("form");
    clear(formHost).append(loading(t("Loading the options…")));
    form = null;
    try {
      const fresh = formBase === null && editor.value === "" && newGame;
      const values = fresh
        ? { game: newGame, name: defaultPlayerName(), description: "", options: {}, others: [] }
        : await post("/api/configs/form/read", { content: editor.value });
      if (!isCurrent() || mode !== "form") return;
      if (values.unavailable) {
        if (opening) { setMode("yaml"); return; }
        clear(formHost).append(banner("info", icon("alert", 16), h("div", { class: "grow" }, tServer(values.unavailable)),
          h("button", { class: "btn sm", type: "button", onclick: () => showYaml() }, t("Open the YAML"))));
        return;
      }
      let schema;
      try {
        schema = await loadSchema(values.game);
      } catch (err) {
        if (!fresh) throw err;
        // No form for this game (its options could not be read): the documented template still works
        location.replace(`#/configs/new?game=${encodeURIComponent(newGame)}&yaml=1`);
        return;
      }
      if (!isCurrent() || mode !== "form") return;
      form = optionsForm(schema, values, { onChange: () => { formDirty = true; } });
      formBase = fresh ? null : editor.value;
      formDirty = false;
      const comments = formBase && /^\s*#|\s#/m.test(formBase);
      clear(formHost).append(
        comments ? h("p", { class: "muted small" }, t("When you change options here, the file is written anew: comments in it are not kept.")) : null,
        form.node);
    } catch (err) {
      if (opening && mode === "form") { setMode("yaml"); toast(err.message, "error"); return; }
      clear(formHost).append(banner("error", icon("alert", 16), h("div", { class: "grow" }, err.message),
        h("button", { class: "btn sm", type: "button", onclick: () => showYaml() }, t("Open the YAML"))));
    }
  }

  async function showYaml() {
    if (mode === "form" && needsWrite()) {
      try {
        editor.value = await writeForm();
      } catch (err) {
        toast(err.message, "error");
        return;
      }
    }
    setMode("yaml");
  }

  formTab.onclick = () => { rememberTab("form"); showForm(); };
  yamlTab.onclick = () => { rememberTab("yaml"); showYaml(); };

  function drawInfo(c, warnings = []) {
    clear(infoHost);
    if (!c) {
      infoHost.append(h("p", { class: "muted small" }, t("Save the file to see which players and games it defines.")));
      return;
    }
    infoHost.append(
      h("dl", { class: "kv" },
        h("dt", {}, t("Player")), h("dd", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, t("taken from the file name"))),
        h("dt", {}, t("Game")), h("dd", {}, chips(c.games, c.missing_games)),
        c.owner && isAdmin() ? [h("dt", {}, t("Owner")), h("dd", {}, c.owner)] : null,
        h("dt", {}, t("Updated")), h("dd", {}, fmtAgo(c.updated_at))),
      ...warnings.map((w) => banner("info", icon("alert", 16), tServer(w))),
      c.missing_games.length ? missingGamesPanel(c.missing_games.map((g) => ({ game: g, configs: [{ id: c.id, name: c.name, owner: c.owner }], games: [], pending: [] })),
        async () => drawInfo(await get(`/api/configs/${c.id}`), warnings)) : null,
      missingRomsPanel(c.missing_roms || []));
  }

  const save = h("button", { class: "btn primary", type: "button" }, icon("check", 14), t("Save"));
  const doSave = action(async () => {
    clear(errorHost);
    try {
      if (mode === "form" && needsWrite()) editor.value = await writeForm();
      const result = id === null
        ? await post("/api/configs/text", { name: nameInput.value, content: editor.value })
        : await put(`/api/configs/${id}`, { name: nameInput.value, content: editor.value });
      saved = { text: editor.value, name: result.name };
      nameInput.value = result.name;
      toast(t("Saved"), "success");
      if (id === null) { location.hash = `#/configs/${result.id}`; return; }
      config = { ...config, ...result };
      drawInfo(config, result.warnings || []);
    } catch (err) {
      errorHost.append(banner("error", icon("alert", 16), err.message));
      throw new Error(t("Not saved"));
    }
  });
  save.onclick = doSave;

  // What the file comes out as when the generator rolls its weights, random ranges and triggers (mystery.py)
  async function rollDialog() {
    if (mode === "form" && needsWrite()) editor.value = await writeForm();
    const source = editor.value;
    const summary = h("div", { class: "stack tight" });
    const out = h("textarea", { class: "input editor", readOnly: true, spellcheck: false, wrap: "off", rows: 14, "aria-label": t("Rolled YAML") });
    let rolled = null;
    const again = h("button", { class: "btn", type: "button" }, icon("dice", 14), t("Roll again"));
    const keep = h("button", { class: "btn primary", type: "button" }, icon("check", 14), t("Save as a new config"));
    const roll = async () => {
      again.disabled = keep.disabled = true;
      try {
        rolled = await post("/api/configs/roll", { content: source });
        clear(summary).append(rolled.rolled.map((p) => h("div", {}, h("span", { class: "slot-name" }, p.name), " · ", p.game)));
        out.value = rolled.yaml;
      } finally {
        again.disabled = keep.disabled = false;
      }
    };
    again.onclick = action(roll);
    keep.onclick = action(async () => {
      const base = nameInput.value.replace(/\.ya?ml$/i, "");
      const result = await post("/api/configs/text", { name: `${base} (${t("rolled")}).yaml`, content: rolled.yaml });
      modal.close();
      toast(t("Saved as {name}", { name: result.name }), "success");
      location.hash = `#/configs/${result.id}`;
    });
    const modal = openModal({
      title: t("Rolled"),
      body: h("div", { class: "stack" },
        h("p", { class: "muted small" }, t("What this file comes out as once its weights, random ranges and triggers are rolled, as the generator does it. Each generation rolls anew; save the result to play exactly this.")),
        summary, out),
      actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close")), again, keep],
    });
    await roll();
  }
  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save.click(); }
  };
  document.addEventListener("keydown", onKey);
  cleanup(() => document.removeEventListener("keydown", onKey));
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {  // keep two-space indents handy
      e.preventDefault();
      const { selectionStart: s, selectionEnd: end } = editor;
      editor.setRangeText("  ", s, end, "end");
    }
  });

  root.append(
    h("div", { class: "muted small" }, h("a", { href: "#/configs" }, t("← All configs"))),
    pageHeader(id === null ? t("New config") : config.name, t("Set the options in the form or edit the YAML directly. Everything is checked when you save."),
      h("button", { class: "btn", type: "button", title: t("See what the weights, random ranges and triggers of this file come out as"),
        onclick: action(rollDialog) }, icon("dice", 14), t("Roll")),
      id !== null ? h("a", { class: "btn", href: `/api/configs/${id}/download` }, icon("download", 14), t("Download")) : null,
      id !== null ? h("button", { class: "btn danger", type: "button", onclick: action(async () => {
        if (!(await confirmDialog(t("Delete {name}?", { name: config.name }), { danger: true, confirmText: t("Delete") }))) return;
        await del(`/api/configs/${id}`);
        saved = { text: editor.value, name: nameInput.value };
        formDirty = false;
        toast(t("Config deleted"), "success");
        location.hash = "#/configs";
      }) }, icon("trash", 14), t("Delete")) : null,
      save),
    errorHost,
    h("div", { class: "editor-layout" },
      h("div", { class: "stack" }, field(t("File name"), nameInput),
        h("div", { class: "tabs", role: "tablist", style: { marginBottom: 0 } }, formTab, yamlTab), formHost, yamlHost),
      h("div", { class: "card stack" }, h("h2", {}, t("Details")), infoHost)));
  drawInfo(config, config ? config.warnings : []);
  if (start === "form") await showForm({ opening: true });
  else setMode("yaml");
}

async function renderPicker(root) {
  const templates = await get("/api/templates");
  const search = h("input", { class: "input", placeholder: t("Search games…"), type: "search", "aria-label": t("Search games") });
  const list = h("div", { class: "grid tiles" });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    clear(list).append(...templates.filter((tpl) => tpl.game.toLowerCase().includes(q)).map((tpl) =>
      h("a", { class: "tile", href: `#/configs/new?game=${encodeURIComponent(tpl.game)}${tpl.form ? "" : "&yaml=1"}`, style: { color: "inherit" } },
        h("div", {}, tpl.game), tpl.source === "custom" ? badge(t("custom"), "accent") : null)));
    if (!list.childElementCount) list.append(h("p", { class: "muted" }, t("No game matches. Custom games can be added under “Custom games”.")));
  };
  search.oninput = draw;
  draw();
  root.append(h("div", { class: "muted small" }, h("a", { href: "#/configs" }, t("← All configs"))),
    pageHeader(t("New config"), t("Pick the game you want to play. You then choose its options in a form, custom games included.")),
    h("div", { class: "stack" }, search, list,
      h("div", { class: "row" }, h("a", { class: "btn", href: "#/configs/new?game=__blank__" }, t("Start with an empty file")),
        h("a", { class: "btn", href: "#/configs?shared=1" }, icon("users", 14), t("Copy a config another player shares")))));
  search.focus();
}
