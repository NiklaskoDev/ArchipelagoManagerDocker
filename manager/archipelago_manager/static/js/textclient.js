// The text client on the game page: chat and the server's commands (!hint, !remaining, ...) in the player's name,
// without Archipelago's own client. The manager holds the connection to the game's server (textclient.py); the page
// asks it for new messages every second or two.
import { h, clear, icon, toast, action } from "./util.js";
import { api, get, del } from "./api.js";
import { badge, checkbox, select } from "./components.js";
import { t, tServer } from "./i18n.js";

const POLL_MS = 1500;
const HISTORY = 50;
// Archipelago's own colors (NetUtils.JSONtoTextParser), on the console's dark background
const COLORS = { black: "#000000", red: "#ee0000", green: "#00ff7f", yellow: "#fafad2", blue: "#6495ed", magenta: "#ee00ee",
  cyan: "#00eeee", slateblue: "#6d8be8", plum: "#af99ef", salmon: "#fa8072", white: "#ffffff", orange: "#ff7700" };
const HINT_STATUS_COLORS = { 0: "white", 10: "slateblue", 20: "salmon", 30: "plum", 40: "green" };

// Whether the client was open, per game and slot, so it opens again after a reload (this browser only)
const remembered = (key) => { try { return localStorage.getItem(key) === "1"; } catch { return false; } };
const remember = (key, open) => { try { open ? localStorage.setItem(key, "1") : localStorage.removeItem(key); } catch { /* private mode */ } };

function partNode(p) {
  if (p.kind === "player") return h("span", { class: p.me ? "tc-me" : "tc-player" }, p.text);
  if (p.kind === "item") return h("span", { class: `tc-item ${p.item}` }, p.text);
  if (p.kind === "location") return h("span", { class: "tc-location" }, p.text);
  if (p.kind === "entrance") return h("span", { class: "tc-entrance" }, p.text);
  if (p.kind === "hint_status") return h("span", { style: { color: COLORS[HINT_STATUS_COLORS[p.status]] || COLORS.red } }, p.text);
  if (p.kind === "color") {
    const codes = (p.color || "").split(";");
    const style = {};
    const color = codes.map((c) => COLORS[c]).find(Boolean);
    if (color) style.color = color;
    if (codes.includes("bold")) style.fontWeight = "600";
    if (codes.includes("underline")) style.textDecoration = "underline";
    return h("span", { style }, p.text);
  }
  return p.text;
}

const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function textClientSection({ gameId, slots, cleanup, isCurrent }) {
  let current = slots[0].slot, after = 0, timer = null, busy = false, open = false, names = null, mode = "";
  const messages = [];
  const history = [];
  let historyIndex = -1;
  const storageKey = () => `apm-textclient-${gameId}-${current}`;
  const url = () => `/api/games/${gameId}/slots/${current}/client`;

  const picker = slots.length > 1 ? select(slots.map((s) => [s.slot, `${s.name} (${s.game})`]), current) : null;
  if (picker) picker.style.maxWidth = "320px";
  const status = h("div", { class: "row" });
  const out = h("div", { class: "console textclient", tabIndex: 0, role: "log", "aria-live": "polite", "aria-label": t("Messages from the server") });
  const onlyMine = checkbox(t("Only messages about me"), false);
  const listId = `tc-${gameId}-${Math.random().toString(36).slice(2)}`;
  const suggestions = h("datalist", { id: listId });
  const input = h("input", { class: "input mono", list: listId, maxLength: 1000, autocomplete: "off",
    placeholder: t("Chat, or a command such as !hint Hookshot"), "aria-label": t("Message or command") });
  const sendButton = h("button", { class: "btn primary", type: "button" }, t("Send"));
  const body = h("div", { class: "stack" });
  const openButton = h("button", { class: "btn primary", type: "button" }, icon("terminal", 14), t("Connect"));
  const closeButton = h("button", { class: "btn sm ghost", type: "button" }, t("Disconnect"));

  function drawStatus(state) {
    const parts = [];
    if (!open) parts.push(h("span", { class: "muted small" }, t("Not connected")));
    else if (state.state === "connected") {
      parts.push(badge(t("Connected"), "ok"));
      if (state.hint_points != null) {
        parts.push(h("span", { class: "muted small" }, state.hint_cost
          ? t("{points} hint points · a hint costs {cost}", { points: state.hint_points, cost: state.hint_cost })
          : t("{points} hint points · hints are free", { points: state.hint_points })));
      }
    } else if (state.state === "offline") parts.push(badge(t("Server not running"), "muted"));
    else if (state.state === "refused") parts.push(badge(t("Refused"), "bad"));
    else parts.push(h("span", { class: "spinner" }), h("span", { class: "muted small" }, t("Connecting…")));
    clear(status).append(parts, open ? closeButton : null);
  }

  function messageNode(m) {
    const cls = m.type === "Client" ? "msg note" : "msg";
    const content = m.type === "Client" ? tServer(m.parts.map((p) => p.text).join("")) : m.parts.map(partNode);
    return h("div", { class: cls, "data-mine": m.mine ? "1" : "0" }, h("span", { class: "time" }, timeOf(m.time)), content);
  }

  function drawMessages(fresh, reset = false) {
    const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
    if (reset) clear(out);
    const shown = fresh.filter((m) => m.mine || !onlyMine.input.checked);
    if (shown.length) out.querySelector(".empty")?.remove();
    out.append(shown.map(messageNode));
    while (out.childElementCount > 600) out.firstElementChild.remove();
    if (!out.childElementCount) out.append(h("div", { class: "msg note empty" }, t("Messages of the multiworld appear here: chat, items being sent, hints.")));
    if (pinned || reset) out.scrollTop = out.scrollHeight;
  }

  async function loadNames() {
    try {
      names = await get(`${url()}/names`);
      if (!names.items.length && !names.locations.length) names = null;
    } catch { names = null; }
  }

  // After "!hint " the datalist offers the player's items, after "!hint_location " their locations
  function syncSuggestions() {
    const value = input.value;
    const next = !names ? "" : value.startsWith("!hint_location ") ? "locations" : value.startsWith("!hint ") ? "items" : "";
    if (next === mode) return;
    mode = next;
    clear(suggestions).append(!next ? [] : names[next].map((name) =>
      h("option", { value: `${next === "items" ? "!hint" : "!hint_location"} ${name}` })));
  }

  async function poll() {
    if (busy || !open) return;
    busy = true;
    try {
      const state = await api("POST", url(), { after });
      if (!isCurrent() || !open) return;
      const reset = state.last < after;  // a new connection on the server: its messages start again at 1
      if (reset) messages.length = 0;
      messages.push(...state.messages);
      if (messages.length > 600) messages.splice(0, messages.length - 600);
      drawMessages(state.messages, reset);
      after = state.last;
      drawStatus(state);
      sendButton.disabled = input.disabled = state.state !== "connected";
      if (state.state === "connected" && !names) await loadNames();
    } catch (err) {
      clear(status).append(h("span", { class: "form-error small" }, err.message), closeButton);
    } finally { busy = false; }
  }

  function start() {
    open = true;
    remember(storageKey(), true);
    clear(body).append(out, h("div", { class: "row spread" }, onlyMine, h("span", { class: "muted small" },
      t("Chat goes to everyone in the multiworld."))),
      h("div", { class: "row" }, h("div", { class: "grow" }, input), sendButton, suggestions),
      h("p", { class: "muted small", style: { margin: 0 } },
        t("The server's commands start with !: !hint with an item gives a hint for it, !hint_location with a location says what is there, !remaining lists what is left in your world, !help lists all of them.")));
    drawMessages([], true);
    drawStatus({ state: "connecting" });
    poll();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
  }

  function stop({ forget }) {
    open = false;
    clearInterval(timer);
    if (forget) remember(storageKey(), false);
    messages.length = 0;
    after = 0;
    names = null;
    mode = "";
    clear(body).append(h("p", { class: "muted", style: { margin: 0 } },
      t("Chat with the multiworld and use the server's commands, such as !hint, right here, without Archipelago's text client. Connecting shows you as “viewing” to the other players.")),
      h("div", {}, openButton));
    drawStatus({});
  }

  async function send() {
    const text = input.value.trim();
    if (!text || !open) return;
    try {
      await api("POST", `${url()}/say`, { text });
      if (history[0] !== text) history.unshift(text);
      history.length = Math.min(history.length, HISTORY);
      historyIndex = -1;
      input.value = "";
      syncSuggestions();
      setTimeout(poll, 250);
    } catch (err) { toast(err.message, "error", 7000); }
  }

  input.addEventListener("input", syncSuggestions);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); send(); }
    else if (e.key === "ArrowUp" && history.length && (historyIndex >= 0 || !input.value)) {
      e.preventDefault();
      historyIndex = Math.min(historyIndex + 1, history.length - 1);
      input.value = history[historyIndex];
    } else if (e.key === "ArrowDown" && historyIndex >= 0) {
      e.preventDefault();
      historyIndex -= 1;
      input.value = historyIndex >= 0 ? history[historyIndex] : "";
    }
  });
  sendButton.onclick = send;
  onlyMine.input.addEventListener("change", () => drawMessages(messages, true));
  openButton.onclick = start;
  closeButton.onclick = action(async () => {
    await del(url()).catch(() => {});  // gone already is fine
    stop({ forget: true });
  });
  if (picker) picker.onchange = () => { stop({ forget: false }); current = Number(picker.value); if (remembered(storageKey())) start(); };
  cleanup(() => clearInterval(timer));  // leaving the page; the server closes the connection when nobody asks any more

  if (remembered(storageKey())) start();
  else stop({ forget: false });
  return h("div", { class: "section" }, h("div", { class: "card stack" },
    h("div", { class: "card-head" }, h("h2", {}, t("Text client")), h("div", { class: "row" }, picker, status)), body));
}
