import { h, clear, icon, toast, action, confirmDialog, fmtDate, fmtDuration } from "../util.js";
import { get, post, del } from "../api.js";
import { pageHeader, jobBadge, chips, banner, badge } from "../components.js";

export default async function render({ root, params, cleanup, isCurrent }) {
  const id = Number(params[0]);
  let job = await get(`/api/jobs/${id}`);
  const out = h("div", { class: "console job-log", tabIndex: 0, "aria-label": "Generator output" });
  const head = h("div");
  const playersHost = h("div");
  let after = 0, timer = null, busy = false;

  const finished = () => !["queued", "running"].includes(job.status);

  function drawHead() {
    const elapsed = job.started_at ? (job.finished_at || Date.now() / 1000) - job.started_at : 0;
    clear(head).append(
      h("div", { class: "muted small" }, h("a", { href: "#/generate" }, "← Generate")),
      pageHeader(job.name || `Job #${job.id}`, [
        `Started by ${job.creator || "unknown"}`, fmtDate(job.created_at), job.started_at ? `took ${fmtDuration(elapsed)}` : null,
      ].filter(Boolean).join(" · "),
        jobBadge(job.status),
        job.queue_position ? badge(`#${job.queue_position} in queue`, "muted") : null,
        !finished() ? h("button", { class: "btn danger", type: "button", onclick: action(async () => {
          if (!(await confirmDialog("Stop this generation?", { danger: true, confirmText: "Stop" }))) return;
          job = await post(`/api/jobs/${id}/cancel`);
          drawHead();
        }) }, icon("stop", 14), "Cancel") : null,
        job.game_id ? h("a", { class: "btn primary", href: `#/games/${job.game_id}` }, icon("server", 14), "Open game") : null,
        finished() ? h("button", { class: "btn ghost", type: "button", onclick: action(async () => {
          if (!(await confirmDialog("Remove this job and its log?", { danger: true, confirmText: "Remove" }))) return;
          await del(`/api/jobs/${id}`);
          location.hash = "#/generate";
        }) }, icon("trash", 14), "Remove") : null),
      job.status === "failed" ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, "Generation failed"), h("pre", { class: "mono", style: { margin: "6px 0 0", whiteSpace: "pre-wrap" } }, job.error || ""))) : null);
  }

  function addLines(lines) {
    const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
    for (const l of lines) out.append(h("div", { class: /error|exception|traceback/i.test(l.text) ? "err" : l.text.startsWith("$ ") ? "cmd" : "" }, l.text));
    if (pinned) out.scrollTop = out.scrollHeight;
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const [fresh, log] = await Promise.all([get(`/api/jobs/${id}`), get(`/api/jobs/${id}/log?after=${after}`)]);
      if (!isCurrent()) return;
      if (log.lines.length) addLines(log.lines);
      after = log.last;
      const changed = fresh.status !== job.status || fresh.queue_position !== job.queue_position;
      job = fresh;
      if (changed || !finished()) drawHead();
      if (finished()) { clearInterval(timer); if (fresh.game_id) toast("Generation finished", "success"); }
    } finally { busy = false; }
  }

  playersHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Player", "Owner", "Games", "File"].map((t) => h("th", {}, t)))),
    h("tbody", {}, job.players.map((p) => h("tr", {},
      h("td", {}, p.names.length ? p.names.join(", ") : h("span", { class: "muted" }, "from file name")),
      h("td", {}, p.owner || "—"), h("td", {}, chips(p.games)), h("td", { class: "mono muted" }, p.filename)))))));
  drawHead();
  root.append(head,
    h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Generator output")), out),
    h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, `Players (${job.players.length})`)), playersHost));
  await poll();
  if (!finished()) timer = setInterval(() => { if (!document.hidden) poll().catch(() => {}); }, 1200);
  cleanup(() => clearInterval(timer));
}
