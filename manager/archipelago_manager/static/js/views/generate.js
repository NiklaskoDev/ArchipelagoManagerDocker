import { h, clear, icon, toast, action, fmtAgo, fmtDuration } from "../util.js";
import { api, get, isAdmin, state } from "../api.js";
import { pageHeader, chips, banner, field, checkbox, select, jobBadge, missingGamesPanel, promptMissingGames, empty } from "../components.js";

const SPOILER_CHOICES = [[1, "Basic spoiler log"], [0, "No spoiler log"], [2, "With playthrough"], [3, "Playthrough and paths"]];
const PLANDO = [["bosses", "Bosses"], ["connections", "Connections"], ["texts", "Texts"], ["items", "Items"]];

export default async function render({ root, cleanup, isCurrent }) {
  const admin = isAdmin();
  const [configs, jobs] = await Promise.all([get(`/api/configs${admin ? "?all=true" : ""}`), get("/api/jobs")]);
  const selected = new Set(new URLSearchParams(location.hash.split("?")[1] || "").getAll("config").map(Number));

  const missingHost = h("div");
  const countLabel = h("span", { class: "muted" });
  const filter = h("input", { class: "input", type: "search", placeholder: "Filter configs…", "aria-label": "Filter configs" });
  const tbody = h("tbody");
  const boxes = new Map();

  function updateSelection() {
    countLabel.textContent = `${selected.size} selected`;
    const chosen = configs.filter((c) => selected.has(c.id));
    const missing = [...new Set(chosen.flatMap((c) => c.missing_games))];
    clear(missingHost).append(missingGamesPanel(missing.map((game) => ({
      game, pending: [], games: [], configs: chosen.filter((c) => c.missing_games.includes(game)).map((c) => ({ id: c.id, name: c.name, owner: c.owner })),
    })), reloadConfigs) || "");
  }
  async function reloadConfigs() {
    const fresh = await get(`/api/configs${admin ? "?all=true" : ""}`);
    if (!isCurrent()) return;
    configs.splice(0, configs.length, ...fresh);
    drawRows();
    updateSelection();
  }
  function drawRows() {
    const q = filter.value.trim().toLowerCase();
    clear(tbody);
    boxes.clear();
    for (const c of configs) {
      const hay = `${c.name} ${c.owner ?? ""} ${c.player_names.join(" ")} ${c.games.join(" ")}`.toLowerCase();
      if (q && !hay.includes(q)) continue;
      const box = h("input", { type: "checkbox", checked: selected.has(c.id), "aria-label": `Include ${c.name}` });
      const sync = () => { box.checked ? selected.add(c.id) : selected.delete(c.id); updateSelection(); };
      box.addEventListener("change", sync);
      boxes.set(c.id, box);
      tbody.append(h("tr", { onclick: (e) => { if (e.target !== box && !e.target.closest("a")) { box.checked = !box.checked; sync(); } }, style: { cursor: "pointer" } },
        h("td", { class: "select-col" }, box),
        admin ? h("td", {}, c.owner) : null,
        h("td", {}, h("span", { class: "slot-name" }, c.name)),
        h("td", {}, c.player_names.length ? c.player_names.join(", ") : h("span", { class: "muted" }, "from file name")),
        h("td", {}, chips(c.games, c.missing_games))));
    }
    if (!tbody.childElementCount) tbody.append(h("tr", {}, h("td", { colspan: 5, class: "muted" }, "Nothing matches.")));
  }
  filter.oninput = drawRows;

  // options -------------------------------------------------------------------------------------------------------------
  const name = h("input", { class: "input", placeholder: "Optional, e.g. Friday run", maxLength: 120 });
  const spoiler = select(SPOILER_CHOICES, 1);
  const seed = h("input", { class: "input", inputMode: "numeric", placeholder: "Random", pattern: "[0-9]*" });
  const race = checkbox("Race mode", false, "Spoiler log is withheld and patches are encrypted where supported.");
  const skipBalance = checkbox("Skip progression balancing", false);
  const plando = PLANDO.map(([key, label]) => ({ key, box: checkbox(label, ["bosses", "connections", "texts"].includes(key)) }));
  const meta = h("textarea", { class: "input", rows: 8, placeholder: "meta_description: My meta file\nnull:\n  progression_balancing: 50", spellcheck: false });
  const hostAfter = checkbox("Start the server when generation finishes", true);
  const errorHost = h("div");
  const submit = h("button", { class: "btn primary", type: "button" }, icon("zap", 14), "Generate");
  submit.onclick = action(async () => {
    clear(errorHost);
    if (!selected.size) throw new Error("Select at least one config");
    if (seed.value && !/^\d+$/.test(seed.value.trim())) throw new Error("The seed must be a number");
    try {
      const job = await api("POST", "/api/jobs", {
        config_ids: [...selected], name: name.value || null, spoiler: Number(spoiler.value), race: race.input.checked,
        seed: seed.value.trim() ? Number(seed.value.trim()) : null, skip_prog_balancing: skipBalance.input.checked,
        plando: plando.filter((p) => p.box.input.checked).map((p) => p.key), meta: meta.value.trim() || null,
        host_after: hostAfter.input.checked,
      });
      location.hash = `#/jobs/${job.id}`;
    } catch (err) {
      errorHost.append(banner("error", icon("alert", 16), err.message));
      if (err.data?.missing_games?.length) {
        const chosen = configs.filter((c) => selected.has(c.id));
        promptMissingGames(err.data.missing_games.map((game) => ({
          game, configs: chosen.filter((c) => c.missing_games.includes(game)).map((c) => ({ id: c.id, name: c.name, owner: c.owner })),
        })), reloadConfigs);
      }
      throw new Error("Generation was not started");
    }
  });

  const jobsHost = h("div");
  function drawJobs(list) {
    clear(jobsHost);
    if (!list.length) { jobsHost.append(h("p", { class: "muted" }, "Nothing generated yet.")); return; }
    jobsHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Job", "Status", "Started", "Result", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, list.slice(0, 12).map((j) => h("tr", {},
        h("td", {}, h("a", { href: `#/jobs/${j.id}`, class: "slot-name" }, j.name || `Job #${j.id}`), admin && j.creator ? h("div", { class: "muted small" }, j.creator) : null),
        h("td", {}, jobBadge(j.status), j.queue_position ? h("span", { class: "muted small" }, ` #${j.queue_position} in queue`) : null),
        h("td", { class: "muted" }, fmtAgo(j.started_at || j.created_at)),
        h("td", {}, j.game_id ? h("a", { href: `#/games/${j.game_id}` }, "Open game") : j.error ? h("span", { class: "muted small" }, j.error.split("\n")[0].slice(0, 70)) : "—"),
        h("td", { class: "actions" }, h("a", { class: "btn sm", href: `#/jobs/${j.id}` }, "Log"))))))));
  }
  async function pollJobs() {
    const list = await get("/api/jobs");
    if (isCurrent()) drawJobs(list);
  }

  if (!configs.length) {
    root.append(pageHeader("Generate", "Create a new multiworld from player configs."),
      empty("No configs to generate from", admin ? "Players can upload their YAML files under “My configs”." : "Upload a config first.",
        h("a", { class: "btn primary", href: "#/configs" }, "Go to configs")));
    root.append(h("div", { class: "section" }, h("h2", {}, "Recent jobs"), jobsHost));
    drawJobs(jobs);
  } else {
    root.append(
      pageHeader("Generate", admin ? "Pick the players for a new multiworld, then generate it." : "Generate a multiworld from your own configs."),
      missingHost,
      h("div", { class: "card flush" },
        h("div", { class: "row spread", style: { padding: "14px 16px" } }, h("div", { class: "grow" }, filter), countLabel),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, [h("th", { class: "select-col" }), admin ? h("th", {}, "Owner") : null, h("th", {}, "Config"), h("th", {}, "Player"), h("th", {}, "Games")])),
          tbody))),
      h("div", { class: "card stack section" },
        h("h2", {}, "Options"),
        h("div", { class: "form-grid" }, field("Name", name, "Shown in the games list"), field("Spoiler log", spoiler), field("Seed", seed, "Leave empty for a random seed")),
        h("div", { class: "stack tight" }, race, skipBalance, hostAfter),
        h("div", { class: "stack tight" }, h("span", { class: "field-label" }, "Allowed plando"), h("div", { class: "row" }, plando.map((p) => p.box))),
        h("details", {}, h("summary", { class: "muted" }, "Advanced: meta.yaml"),
          h("div", { class: "stack", style: { marginTop: "10px" } },
            h("p", { class: "muted small" }, "A meta file can override player options for everyone. It must contain a meta_description."), meta)),
        errorHost, h("div", { class: "row" }, submit)),
      h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Recent jobs")), jobsHost));
    drawRows();
    updateSelection();
    drawJobs(jobs);
  }
  const timer = setInterval(() => { if (!document.hidden) pollJobs().catch(() => {}); }, 3000);
  cleanup(() => clearInterval(timer));
}
