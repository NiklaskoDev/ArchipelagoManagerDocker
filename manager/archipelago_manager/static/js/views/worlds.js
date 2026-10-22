import { h, clear, icon, toast, action, confirmDialog, fmtBytes, fmtAgo } from "../util.js";
import { get, post, del, isAdmin, state } from "../api.js";
import { pageHeader, badge, banner, missingGamesPanel, uploadApworldModal, githubSourceModal, canSubmitApworld, empty } from "../components.js";
import { t, plural, tServer } from "../i18n.js";

export default async function render({ root, isCurrent }) {
  const admin = isAdmin();
  const missingHost = h("div");
  const customHost = h("div");
  const sourcesHost = h("div");
  const allHost = h("div");
  let worlds = null;

  async function load() {
    const [apworlds, missing, w, sources] = await Promise.all([get("/api/apworlds"), get("/api/missing-games"), get("/api/worlds"),
      get("/api/apworld-sources")]);
    if (!isCurrent()) return;
    worlds = w;
    clear(missingHost).append(missingGamesPanel(missing, load) || "");
    drawCustom(apworlds);
    drawSources(sources);
    drawAll();
  }

  function sourceStatus(s) {
    if (s.status === "pending") return badge(t("Waiting for approval"), "warn");
    if (s.update_available) return badge(t("{version} available", { version: s.latest_tag }), "warn");
    return s.installed ? badge(t("Up to date"), "ok") : badge(t("Not installed"), "muted");
  }

  function drawSources(list) {
    clear(sourcesHost);
    if (!list.length) return;
    const run = (verb, s, done) => action(async () => {
      const r = await post(`/api/apworld-sources/${s.id}/${verb}`);
      toast(done(r), "success");
      await load();
    });
    sourcesHost.append(h("div", { class: "section-head" }, h("h2", {}, t("From GitHub"))),
      h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, [t("Repository"), t("Game"), t("Installed"), t("Status"), t("Added by"), ""].map((text) => h("th", {}, text)))),
        h("tbody", {}, list.map((s) => h("tr", {},
          h("td", {}, h("a", { href: s.repo_url, target: "_blank", rel: "noopener", class: "slot-name" }, s.repo),
            s.asset ? h("div", { class: "muted small mono" }, s.asset) : null,
            s.note ? h("div", { class: "small" }, `“${s.note}”`) : null,
            s.error ? h("div", { class: "form-error small" }, tServer(s.error)) : null),
          h("td", {}, s.game || h("span", { class: "muted" }, "—")),
          h("td", {}, s.installed_tag || "—", s.latest_tag && s.latest_tag !== s.installed_tag
            ? h("div", { class: "muted small" }, t("newest:"), " ", s.latest_url ? h("a", { href: s.latest_url, target: "_blank", rel: "noopener" }, s.latest_tag) : s.latest_tag) : null),
          h("td", {}, sourceStatus(s), s.checked_at ? h("div", { class: "muted small" }, t("checked {ago}", { ago: fmtAgo(s.checked_at) })) : null),
          h("td", {}, s.adder || "—", h("div", { class: "muted small" }, fmtAgo(s.added_at))),
          h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
            admin && s.status === "pending" ? h("button", { class: "btn sm primary", type: "button",
              onclick: run("approve", s, (r) => t("{game} installed from {repo}", { game: r.game, repo: r.repo })) }, icon("check", 14), t("Approve")) : null,
            admin && s.update_available ? h("button", { class: "btn sm primary", type: "button",
              onclick: run("update", s, (r) => t("{game} updated to {version}", { game: r.game, version: r.installed_tag })) }, icon("download", 14), t("Update")) : null,
            admin && s.status === "approved" ? h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Check for a new release"),
              "aria-label": t("Check for a new release"), onclick: run("check", s, (r) => r.update_available ? t("{version} is available", { version: r.latest_tag }) : t("No new release")) }, icon("restart", 14)) : null,
            (admin || (s.status === "pending" && s.added_by === state.user.id)) ? h("button", { class: "btn sm ghost icon-only", type: "button",
              title: t("Remove"), "aria-label": t("Remove"), onclick: action(async () => {
                const text = s.status === "pending" ? t("Reject this repository? Nothing was downloaded from it.")
                  : t("Forget this repository? A game installed from it stays installed; remove it above if you want it gone.");
                if (!(await confirmDialog(text, { danger: true, confirmText: s.status === "pending" ? t("Reject") : t("Forget") }))) return;
                await del(`/api/apworld-sources/${s.id}`);
                await load();
              }) }, icon("trash", 14)) : null))))))));
  }

  function stateBadge(a) {
    if (a.status === "pending") return badge(t("Waiting for approval"), "warn");
    if (a.load_error) return h("span", { title: tServer(a.load_error) }, badge(t("Not loaded"), "bad"));
    return badge(t("Installed"), "ok");
  }

  function drawCustom(data) {
    clear(customHost);
    if (!data.items.length) {
      customHost.append(empty(t("No custom games yet"),
        t("Games that are not part of Archipelago itself come as .apworld files. Add one to generate for it or to share it with players."),
        canSubmitApworld() ? h("button", { class: "btn primary", type: "button", onclick: () => uploadApworldModal({ onDone: load }) },
          icon("upload", 14), admin ? t("Add APWorld") : t("Submit APWorld")) : null));
      return;
    }
    customHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, [t("Game"), t("Version"), t("Status"), t("Added by"), t("Size"), ""].map((text) => h("th", {}, text)))),
      h("tbody", {}, data.items.map((a) => h("tr", {},
        h("td", {}, h("span", { class: "slot-name" }, a.game), h("div", { class: "muted small mono" }, a.module + ".apworld"),
          a.load_error ? h("div", { class: "form-error small" }, tServer(a.load_error)) : null),
        h("td", {}, a.version || "—"), h("td", {}, stateBadge(a)),
        h("td", {}, a.uploader || "—", h("div", { class: "muted small" }, fmtAgo(a.uploaded_at))),
        h("td", { class: "muted" }, fmtBytes(a.size)),
        h("td", { class: "actions" }, h("div", { class: "row", style: { justifyContent: "flex-end" } },
          a.status === "pending" && admin ? h("button", { class: "btn sm primary", type: "button", onclick: action(async () => {
            const r = await post(`/api/apworlds/${a.id}/approve`);
            toast(t("{game} installed", { game: r.game }), "success");
            if (r.load_error) toast(t("It did not load: {error}", { error: tServer(r.load_error) }), "error");
            await load();
          }) }, icon("check", 14), t("Approve")) : null,
          h("a", { class: "btn sm ghost icon-only", href: `/api/apworlds/${a.id}/download`, title: t("Download"), "aria-label": t("Download") }, icon("download", 14)),
          (admin || (a.status === "pending" && a.uploaded_by === state.user.id)) ? h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Remove"), "aria-label": t("Remove"),
            onclick: action(async () => {
              const what = a.status === "pending" ? t("Reject this submission?")
                : t("Remove {game}? Configs for it will stop working until it is added again.", { game: a.game });
              if (!(await confirmDialog(what, { danger: true, confirmText: a.status === "pending" ? t("Reject") : t("Remove") }))) return;
              await del(`/api/apworlds/${a.id}`);
              await load();
            }) }, icon("trash", 14)) : null))))))));
  }

  function drawAll() {
    clear(allHost);
    if (!worlds) return;
    if (!worlds.available) {
      allHost.append(banner("error", icon("alert", 18), tServer(worlds.error) || t("Archipelago is not available.")));
      return;
    }
    const search = h("input", { class: "input", type: "search", placeholder: plural(worlds.games.length, "Search {n} game…", "Search {n} games…"), "aria-label": t("Search games") });
    const list = h("div", { class: "chips" });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      clear(list).append(...worlds.games.filter((g) => g.name.toLowerCase().includes(q)).map((g) =>
        h("span", { class: `chip ${g.source === "custom" ? "" : ""}`, title: g.source === "custom" ? t("Custom APWorld") : t("Built into Archipelago") },
          g.name, g.source === "custom" ? badge(t("custom"), "accent") : null)));
    };
    search.oninput = draw;
    draw();
    allHost.append(h("div", { class: "card stack" },
      h("div", { class: "row spread" }, h("h2", {}, t("Supported games ({n})", { n: worlds.games.length })), h("span", { class: "muted small" }, `Archipelago ${worlds.ap_version}`)),
      search, list));
  }

  root.append(
    pageHeader(t("Custom games"), t("Games outside of Archipelago's own list are added as .apworld files."),
      canSubmitApworld() ? h("button", { class: "btn", type: "button", onclick: () => githubSourceModal({ onDone: load }) },
        icon("external", 14), t("From GitHub")) : null,
      canSubmitApworld() ? h("button", { class: "btn primary", type: "button", onclick: () => uploadApworldModal({ onDone: load }) },
        icon("upload", 14), admin ? t("Add APWorld") : t("Submit APWorld")) : null),
    missingHost,
    !admin ? banner("info", icon("alert", 16), t("Games and GitHub repositories you submit are reviewed by an administrator before they can be used. Players can download installed APWorlds below to play them locally.")) : null,
    h("div", { class: "section" }, customHost),
    h("div", { class: "section" }, sourcesHost),
    h("div", { class: "section" }, allHost));
  await load();
}
