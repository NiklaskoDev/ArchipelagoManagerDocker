import { h, clear, icon, toast, action, confirmDialog, fmtDate, fmtDuration } from "../util.js";
import { get, post, del } from "../api.js";
import { pageHeader, jobBadge, chips, banner, badge } from "../components.js";
import { t, tServer } from "../i18n.js";

export default async function render({ root, params, cleanup, isCurrent }) {
  const id = Number(params[0]);
  let job = await get(`/api/jobs/${id}`);
  const out = h("div", { class: "console job-log", tabIndex: 0, "aria-label": t("Generator output") });
  const head = h("div");
  const playersHost = h("div");
  let after = 0, timer = null, busy = false;

  const finished = () => !["queued", "running"].includes(job.status);

  function drawHead() {
    const elapsed = job.started_at ? (job.finished_at || Date.now() / 1000) - job.started_at : 0;
    clear(head).append(
      h("div", { class: "muted small" }, h("a", { href: "#/generate" }, t("← Generate"))),
      pageHeader(job.name || t("Job #{id}", { id: job.id }), [
        t("Started by {name}", { name: job.creator || t("unknown") }), fmtDate(job.created_at),
        job.started_at ? t("took {duration}", { duration: fmtDuration(elapsed) }) : null,
      ].filter(Boolean).join(" · "),
        jobBadge(job.status),
        job.queue_position ? badge(t("#{n} in queue", { n: job.queue_position }), "muted") : null,
        !finished() ? h("button", { class: "btn danger", type: "button", onclick: action(async () => {
          if (!(await confirmDialog(t("Stop this generation?"), { danger: true, confirmText: t("Stop") }))) return;
          job = await post(`/api/jobs/${id}/cancel`);
          drawHead();
        }) }, icon("stop", 14), t("Cancel")) : null,
        job.game_id ? h("a", { class: "btn primary", href: `#/games/${job.game_id}` }, icon("server", 14), t("Open game")) : null,
        finished() ? h("button", { class: "btn ghost", type: "button", onclick: action(async () => {
          if (!(await confirmDialog(t("Remove this job and its log?"), { danger: true, confirmText: t("Remove") }))) return;
          await del(`/api/jobs/${id}`);
          location.hash = "#/generate";
        }) }, icon("trash", 14), t("Remove")) : null),
      job.status === "failed" ? banner("error", icon("alert", 18), h("div", { class: "grow" },
        h("strong", {}, t("Generation failed")), h("pre", { class: "mono", style: { margin: "6px 0 0", whiteSpace: "pre-wrap" } }, tServer(job.error) || ""))) : null);
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
      if (finished()) { clearInterval(timer); if (fresh.game_id) toast(t("Generation finished"), "success"); }
    } finally { busy = false; }
  }

  playersHost.append(h("div", { class: "card flush table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, [t("Player"), t("Owner"), t("Games"), t("File")].map((text) => h("th", {}, text)))),
    h("tbody", {}, job.players.map((p) => h("tr", {},
      h("td", {}, p.names.length ? p.names.join(", ") : h("span", { class: "muted" }, t("from file name"))),
      h("td", {}, p.owner || "—"), h("td", {}, chips(p.games)), h("td", { class: "mono muted" }, p.filename)))))));
  drawHead();
  root.append(head,
    h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Generator output"))), out),
    h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, t("Players ({n})", { n: job.players.length }))), playersHost));
  await poll();
  if (!finished()) timer = setInterval(() => { if (!document.hidden) poll().catch(() => {}); }, 1200);
  cleanup(() => clearInterval(timer));
}
