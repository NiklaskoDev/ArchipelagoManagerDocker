import { h, clear, icon, toast, action, confirmDialog, fmtBytes, fmtAgo } from "../util.js";
import { get, post, del, isAdmin, state } from "../api.js";
import { pageHeader, badge, banner, missingGamesPanel, uploadApworldModal, canSubmitApworld, empty } from "../components.js";

export default async function render({ root, isCurrent }) {
  const admin = isAdmin();
  const missingHost = h("div");
  const customHost = h("div");
  const allHost = h("div");
  let worlds = null;

  async function load() {
    const [apworlds, missing, w] = await Promise.all([get("/api/apworlds"), get("/api/missing-games"), get("/api/worlds")]);
    if (!isCurrent()) return;
    worlds = w;
    clear(missingHost).append(missingGamesPanel(missing, load) || "");
    drawCustom(apworlds);
    drawAll();
  }

  function stateBadge(a) {
    if (a.status === "pending") return badge("Waiting for approval", "warn");
    if (a.load_error) return h("span", { title: a.load_error }, badge("Not loaded", "bad"));
    return badge("Installed", "ok");
  }

  function drawCustom(data) {
    clear(customHost);
    if (!data.items.length) {
      customHost.append(empty("No custom games yet",
        "Games that are not part of Archipelago itself come as .apworld files. Add one to generate for it or to share it with players.",
        canSubmitApworld() ? h("button", { class: "btn primary", type: "button", onclick: () => uploadApworldModal({ onDone: load }) },
          icon("upload", 14), admin ? "Add APWorld" : "Submit APWorld") : null));
      return;
    }
    customHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Game", "Version", "Status", "Added by", "Size", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.items.map((a) => h("tr", {},
        h("td", {}, h("span", { class: "slot-name" }, a.game), h("div", { class: "muted small mono" }, a.module + ".apworld"),
          a.load_error ? h("div", { class: "form-error small" }, a.load_error) : null),
        h("td", {}, a.version || "—"), h("td", {}, stateBadge(a)),
        h("td", {}, a.uploader || "—", h("div", { class: "muted small" }, fmtAgo(a.uploaded_at))),
        h("td", { class: "muted" }, fmtBytes(a.size)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          a.status === "pending" && admin ? h("button", { class: "btn sm primary", type: "button", onclick: action(async () => {
            const r = await post(`/api/apworlds/${a.id}/approve`);
            toast(`${r.game} installed`, "success");
            if (r.load_error) toast(`It did not load: ${r.load_error}`, "error");
            await load();
          }) }, icon("check", 14), "Approve") : null,
          h("a", { class: "btn sm ghost icon-only", href: `/api/apworlds/${a.id}/download`, title: "Download", "aria-label": "Download" }, icon("download", 14)),
          (admin || (a.status === "pending" && a.uploaded_by === state.user.id)) ? h("button", { class: "btn sm ghost icon-only", type: "button", title: "Remove", "aria-label": "Remove",
            onclick: action(async () => {
              const what = a.status === "pending" ? "Reject this submission?" : `Remove ${a.game}? Configs for it will stop working until it is added again.`;
              if (!(await confirmDialog(what, { danger: true, confirmText: a.status === "pending" ? "Reject" : "Remove" }))) return;
              await del(`/api/apworlds/${a.id}`);
              await load();
            }) }, icon("trash", 14)) : null))))))));
  }

  function drawAll() {
    clear(allHost);
    if (!worlds) return;
    if (!worlds.available) {
      allHost.append(banner("error", icon("alert", 18), worlds.error || "Archipelago is not available."));
      return;
    }
    const search = h("input", { class: "input", type: "search", placeholder: `Search ${worlds.games.length} games…`, "aria-label": "Search games" });
    const list = h("div", { class: "chips" });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      clear(list).append(...worlds.games.filter((g) => g.name.toLowerCase().includes(q)).map((g) =>
        h("span", { class: `chip ${g.source === "custom" ? "" : ""}`, title: g.source === "custom" ? "Custom APWorld" : "Built into Archipelago" },
          g.name, g.source === "custom" ? badge("custom", "accent") : null)));
    };
    search.oninput = draw;
    draw();
    allHost.append(h("div", { class: "card stack" },
      h("div", { class: "row spread" }, h("h2", {}, `Supported games (${worlds.games.length})`), h("span", { class: "muted small" }, `Archipelago ${worlds.ap_version}`)),
      search, list));
  }

  root.append(
    pageHeader("Custom games", "Games outside of Archipelago's own list are added as .apworld files.",
      canSubmitApworld() ? h("button", { class: "btn primary", type: "button", onclick: () => uploadApworldModal({ onDone: load }) },
        icon("upload", 14), admin ? "Add APWorld" : "Submit APWorld") : null),
    missingHost,
    !admin ? banner("info", icon("alert", 16), "Games you submit are reviewed by an administrator before they can be used. Players can download installed APWorlds below to play them locally.") : null,
    h("div", { class: "section" }, customHost),
    h("div", { class: "section" }, allHost));
  await load();
}
