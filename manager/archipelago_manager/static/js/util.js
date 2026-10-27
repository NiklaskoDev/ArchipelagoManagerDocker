// Small DOM helpers. Everything user-supplied goes through text nodes, never innerHTML.
import { t, plural, locale } from "./i18n.js";

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "for") el.htmlFor = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in el && key !== "list") el[key] = value;  // input.list is a read-only property; the attribute sets it
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

// The views pass optional children (`cond ? node : null`) and arrays to append(). The native method would render those
// as the text "null" or "[object ...]", so make it skip empty values and flatten arrays for this app.
const nativeAppend = Element.prototype.append;
Element.prototype.append = function (...nodes) {
  return nativeAppend.apply(this, nodes.flat(Infinity).filter((n) => n != null && n !== false));
};

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

// On a phone a table shows each row as a card of "label: value" lines (style.css). The labels are the column headers,
// copied into every cell as data-label whenever a table or a row appears, so the views need not repeat them.
function labelCells(table) {
  const heads = [...table.querySelectorAll(":scope > thead > tr > th")]
    .flatMap((th) => Array(th.colSpan).fill(th.textContent.trim()));
  for (const row of table.querySelectorAll(":scope > tbody > tr")) {
    let column = 0;
    for (const cell of row.cells) {
      const label = cell.colSpan === 1 ? heads[column] : "";  // a cell across the row (a note) has no column
      if (label) cell.dataset.label = label;
      else delete cell.dataset.label;
      column += cell.colSpan;
    }
  }
}

new MutationObserver((records) => {
  const tables = new Set();
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      const around = node.closest("table.table");
      if (around) tables.add(around);
      for (const inside of node.querySelectorAll("table.table")) tables.add(inside);
    }
  }
  tables.forEach(labelCells);
}).observe(document.documentElement, { childList: true, subtree: true });

// A translated sentence with elements in it, such as a game name in <strong>: withNodes(text, { game: h("strong", {}, name) })
// puts the element where the text has {game}. Translate the text without values, so its placeholders are still there.
export function withNodes(text, nodes) {
  return text.split(/\{(\w+)\}/).map((piece, i) => (i % 2 ? (piece in nodes ? nodes[piece] : `{${piece}}`) : piece))
    .filter((piece) => piece !== "");
}

// ---------------------------------------------------------------------------------------------------------------------
// icons (Feather-style, 24x24 stroke icons)
// ---------------------------------------------------------------------------------------------------------------------
const ICONS = {
  play: '<polygon points="5 3 19 12 5 21 5 3"/>',
  stop: '<rect x="4" y="4" width="16" height="16" rx="2"/>',
  restart: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  package: '<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
  zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  globe: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  home: '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  key: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  login: '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>',
  dice: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.2" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  menu: '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
};

export function icon(name, size = 16) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  svg.innerHTML = ICONS[name] || "";  // static, trusted markup from the table above
  return svg;
}

// ---------------------------------------------------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------------------------------------------------
export function fmtBytes(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  const digits = n >= 10 || i === 0 ? 0 : 1;
  return `${n.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${units[i]}`;
}

// Shortens a long file name in the middle so the extension, which says what opens the file, stays visible.
export function shortName(name, max = 34) {
  if (name.length <= max) return name;
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 && name.length - dot <= 12 ? name.slice(dot) : "";
  return `${name.slice(0, max - ext.length - 1)}…${ext}`;
}

export function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" }) : "—";
}

// "3 min ago". The singular of the days has its own text: German says "vor 1 Tag" but "vor 4 Tagen".
export function fmtAgo(ts) {
  if (!ts) return t("never");
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return t("just now");
  if (s < 3600) return t("{n} min ago", { n: Math.floor(s / 60) });
  if (s < 86400) return t("{n} h ago", { n: Math.floor(s / 3600) });
  return plural(Math.floor(s / 86400), "1 d ago", "{n} d ago");
}

// "in 2 d 4 h", for deadlines
export function fmtUntil(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return t("passed");
  if (s < 3600) return t("in {n} min", { n: Math.max(1, Math.round(s / 60)) });
  if (s < 86400) return t("in {n} h", { n: Math.round(s / 3600) });
  const days = Math.floor(s / 86400), hours = Math.round((s % 86400) / 3600);
  return days < 3 && hours ? plural(days, "in 1 d {hours} h", "in {n} d {hours} h", { hours }) : plural(days, "in 1 d", "in {n} d");
}

// <input type="datetime-local"> works in local time without a zone; these convert to and from Unix seconds.
export function toLocalInput(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fromLocalInput(value) {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time / 1000;
}

// A playing time: "2 d 5 h", "3 h 12 min", "40 min"
export function fmtSpan(seconds) {
  const minutes = Math.max(0, Math.floor(seconds / 60));
  const d = Math.floor(minutes / 1440), hrs = Math.floor(minutes / 60) % 24, m = minutes % 60;
  if (d) return t("{d} d {h} h", { d, h: hrs });
  if (hrs) return t("{h} h {m} min", { h: hrs, m });
  return t("{m} min", { m });
}

export function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? t("{m}m {s}s", { m, s: String(s).padStart(2, "0") }) : t("{s}s", { s });
}

// ---------------------------------------------------------------------------------------------------------------------
// feedback: toasts, modals, confirmation
// ---------------------------------------------------------------------------------------------------------------------
let toastHost;

export function toast(message, kind = "info", ms = 4500) {
  if (!toastHost) {
    toastHost = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const el = h("div", { class: `toast ${kind}` }, message);
  toastHost.append(el);
  setTimeout(() => el.remove(), kind === "error" ? Math.max(ms, 8000) : ms);
}

export function openModal({ title, body, actions = [], wide = false, onClose }) {
  const previouslyFocused = document.activeElement;
  const close = () => {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    previouslyFocused?.focus?.();
    onClose?.();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const dialog = h("div", { class: `modal ${wide ? "wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div", { class: "modal-head" }, h("h2", {}, title),
      h("button", { class: "btn ghost icon-only", type: "button", "aria-label": t("Close"), onclick: close }, icon("x"))),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("div", { class: "modal-actions" }, actions) : null);
  const overlay = h("div", { class: "overlay", onmousedown: (e) => { if (e.target === overlay) close(); } }, dialog);
  document.body.append(overlay);
  document.addEventListener("keydown", onKey);
  dialog.querySelector("input, textarea, select, button.primary")?.focus();
  return { close, dialog };
}

export function confirmDialog(message, { title = t("Are you sure?"), confirmText = t("Confirm"), danger = false } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (!settled) { settled = true; resolve(value); } modal.close(); };
    const modal = openModal({
      title, body: h("p", {}, message), onClose: () => { if (!settled) { settled = true; resolve(false); } },
      actions: [
        h("button", { class: "btn", type: "button", onclick: () => finish(false) }, t("Cancel")),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", onclick: () => finish(true) }, confirmText),
      ],
    });
  });
}

export async function copyText(text, label = t("Copied")) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      // Plain-http LAN installs have no clipboard API, so fall back to the old selection trick.
      const area = h("textarea", { value: text, style: { position: "fixed", opacity: "0" } });
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      if (!ok) throw new Error("copy failed");
    }
    toast(label, "success", 1800);
  } catch {
    toast(t("Could not copy. Select the text and press Ctrl+C."), "error");
  }
}

// Click handler wrapper: disables the button while `fn` runs and reports failures as a toast.
export function action(fn) {
  return async (event) => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true;
    try {
      await fn(event);
    } catch (err) {
      toast(err.message || String(err), "error");
    } finally {
      button.disabled = false;
    }
  };
}
