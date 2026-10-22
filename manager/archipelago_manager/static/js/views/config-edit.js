import { h, clear, icon, toast, action, confirmDialog, fmtAgo } from "../util.js";
import { get, post, put, del, isAdmin, state } from "../api.js";
import { pageHeader, chips, banner, field, loading, missingGamesPanel, missingRomsPanel, badge } from "../components.js";

export default async function render({ root, params, query, cleanup, setDirtyCheck, isCurrent }) {
  const id = params[0] ? Number(params[0]) : null;
  if (id === null && !query.get("game")) return renderPicker(root);

  let config = null;
  let text;
  let name;
  if (id !== null) {
    config = await get(`/api/configs/${id}`);
    text = config.content;
    name = config.name;
  } else {
    const game = query.get("game");
    text = game === "__blank__"
      ? "name: Player\ngame: \n"
      : await fetch(`/api/templates/yaml?game=${encodeURIComponent(game)}`, { credentials: "same-origin" }).then((r) => {
          if (!r.ok) throw new Error(`No template for ${game}`);
          return r.text();
        });
    // templates ship with a placeholder name; give the user a sensible starting point
    text = text.replace(/^name: .*$/m, `name: ${state.user.username.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 16) || "Player"}`);
    name = `${game.replace(/[^A-Za-z0-9 _-]+/g, "").trim() || "config"}.yaml`;
  }

  const editor = h("textarea", { class: "input editor", spellcheck: false, wrap: "off", "aria-label": "YAML" });
  editor.value = text;
  const nameInput = h("input", { class: "input", value: name, maxLength: 120, "aria-label": "File name" });
  const infoHost = h("div", { class: "stack" });
  const errorHost = h("div");
  let saved = { text, name };
  const dirty = () => editor.value !== saved.text || nameInput.value !== saved.name;
  setDirtyCheck(dirty);
  const onBeforeUnload = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onBeforeUnload);
  cleanup(() => window.removeEventListener("beforeunload", onBeforeUnload));

  function drawInfo(c, warnings = []) {
    clear(infoHost);
    if (!c) {
      infoHost.append(h("p", { class: "muted small" }, "Save the file to see which players and games it defines."));
      return;
    }
    infoHost.append(
      h("dl", { class: "kv" },
        h("dt", {}, "Player"), h("dd", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, "taken from the file name")),
        h("dt", {}, "Game"), h("dd", {}, chips(c.games, c.missing_games)),
        c.owner && isAdmin() ? [h("dt", {}, "Owner"), h("dd", {}, c.owner)] : null,
        h("dt", {}, "Updated"), h("dd", {}, fmtAgo(c.updated_at))),
      ...warnings.map((w) => banner("info", icon("alert", 16), w)),
      c.missing_games.length ? missingGamesPanel(c.missing_games.map((g) => ({ game: g, configs: [{ id: c.id, name: c.name, owner: c.owner }], games: [], pending: [] })),
        async () => drawInfo(await get(`/api/configs/${c.id}`), warnings)) : null,
      missingRomsPanel(c.missing_roms || []));
  }

  const save = h("button", { class: "btn primary", type: "button" }, icon("check", 14), "Save");
  const doSave = action(async () => {
    clear(errorHost);
    try {
      const result = id === null
        ? await post("/api/configs/text", { name: nameInput.value, content: editor.value })
        : await put(`/api/configs/${id}`, { name: nameInput.value, content: editor.value });
      saved = { text: editor.value, name: result.name };
      nameInput.value = result.name;
      toast("Saved", "success");
      if (id === null) { location.hash = `#/configs/${result.id}`; return; }
      config = { ...config, ...result };
      drawInfo(config, result.warnings || []);
    } catch (err) {
      errorHost.append(banner("error", icon("alert", 16), err.message));
      throw new Error("Not saved");
    }
  });
  save.onclick = doSave;
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
    h("div", { class: "muted small" }, h("a", { href: "#/configs" }, "← All configs")),
    pageHeader(id === null ? "New config" : config.name, "Edit the YAML directly. Everything is checked when you save.",
      id !== null ? h("a", { class: "btn", href: `/api/configs/${id}/download` }, icon("download", 14), "Download") : null,
      id !== null ? h("button", { class: "btn danger", type: "button", onclick: action(async () => {
        if (!(await confirmDialog(`Delete ${config.name}?`, { danger: true, confirmText: "Delete" }))) return;
        await del(`/api/configs/${id}`);
        saved = { text: editor.value, name: nameInput.value };
        toast("Config deleted", "success");
        location.hash = "#/configs";
      }) }, icon("trash", 14), "Delete") : null,
      save),
    errorHost,
    h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0, 1fr) minmax(260px, 340px)", alignItems: "start" } },
      h("div", { class: "stack" }, field("File name", nameInput), editor),
      h("div", { class: "card stack" }, h("h2", {}, "Details"), infoHost)));
  drawInfo(config, config ? config.warnings : []);
}

async function renderPicker(root) {
  const templates = await get("/api/templates");
  const search = h("input", { class: "input", placeholder: "Search games…", type: "search", "aria-label": "Search games" });
  const list = h("div", { class: "grid tiles" });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    clear(list).append(...templates.filter((t) => t.game.toLowerCase().includes(q)).map((t) =>
      h("a", { class: "tile", href: `#/configs/new?game=${encodeURIComponent(t.game)}`, style: { color: "inherit" } },
        h("div", {}, t.game), t.source === "custom" ? badge("custom", "accent") : null)));
    if (!list.childElementCount) list.append(h("p", { class: "muted" }, "No game matches. Custom games can be added under “Custom games”."));
  };
  search.oninput = draw;
  draw();
  root.append(h("div", { class: "muted small" }, h("a", { href: "#/configs" }, "← All configs")),
    pageHeader("New config", "Pick the game you want to play. You get a documented template with every option."),
    h("div", { class: "stack" }, search, list,
      h("div", {}, h("a", { class: "btn", href: "#/configs/new?game=__blank__" }, "Start with an empty file"))));
  search.focus();
}
