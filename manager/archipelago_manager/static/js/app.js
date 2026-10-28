import { h, clear, icon, toast, openModal, action, confirmDialog, fmtDate } from "./util.js";
import { api, state, refreshStatus, setUnauthorizedHandler, isAdmin, canGenerate, post, put, ApiError } from "./api.js";
import { banner, loading, field, checkbox } from "./components.js";
import { t, plural, lang, locale, LANGUAGES, setLanguage } from "./i18n.js";

// path, page, section of the nav, and the title of the browser tab (a page with a name of its own sets it: setTitle)
const ROUTES = [
  [/^\/?$/, () => import("./views/dashboard.js"), "home", () => t("Dashboard")],
  [/^\/games$/, () => import("./views/games.js"), "games", () => t("Games")],
  [/^\/games\/(\d+)$/, () => import("./views/game.js"), "games", () => t("Games")],
  [/^\/archive$/, () => import("./views/archive.js"), "games", () => t("Archive")],
  [/^\/configs$/, () => import("./views/configs.js"), "configs", () => (isAdmin() ? t("Player configs") : t("My configs"))],
  [/^\/configs\/new$/, () => import("./views/config-edit.js"), "configs", () => t("New config")],
  [/^\/configs\/(\d+)$/, () => import("./views/config-edit.js"), "configs", () => t("My configs")],
  [/^\/generate$/, () => import("./views/generate.js"), "generate", () => t("Generate")],
  [/^\/jobs\/(\d+)$/, () => import("./views/job.js"), "generate", () => t("Generation")],
  [/^\/signups$/, () => import("./views/signups.js"), "signups", () => t("Sign-ups")],
  [/^\/signups\/(\d+)$/, () => import("./views/signup.js"), "signups", () => t("Sign-ups")],
  [/^\/worlds$/, () => import("./views/worlds.js"), "worlds", () => t("Custom games")],
  [/^\/admin\/users$/, () => import("./views/admin-users.js"), "admin", () => t("Users")],
  [/^\/admin\/settings$/, () => import("./views/admin-settings.js"), "admin", () => t("Settings")],
  [/^\/admin\/log$/, () => import("./views/admin-log.js"), "admin", () => t("Log")],
  [/^\/tokens$/, () => import("./views/tokens.js"), null, () => t("API tokens")],
];

const app = document.getElementById("app");
let view = null;          // the container of the current page
let cleanups = [];
let dirtyCheck = null;
let currentHash = null;
let navToken = 0;

// ---------------------------------------------------------------------------------------------------------------------
// router
// ---------------------------------------------------------------------------------------------------------------------
function runCleanups() {
  for (const fn of cleanups.splice(0)) {
    try { fn(); } catch (e) { console.error(e); }
  }
  dirtyCheck = null;
}

// The browser tab's title: the page, and in front of it how many new messages a page counted while the tab was in the
// background (the game page: what others said, sent or hinted to the player)
let pageTitle = "", unread = 0;
function drawTitle() {
  document.title = `${unread ? `(${unread}) ` : ""}${pageTitle ? `${pageTitle} · ` : ""}Archipelago Manager`;
}
function setTitle(text, count = 0) {
  pageTitle = text;
  unread = count;
  drawTitle();
}

async function route() {
  const hash = location.hash || "#/";
  if (dirtyCheck?.() && currentHash !== null && hash !== currentHash) {
    if (!confirm(t("You have unsaved changes. Leave this page anyway?"))) {
      history.replaceState(null, "", currentHash);
      return;
    }
  }
  if (linkInAddress()) {  // an invite or reset link pasted while the app is open
    runCleanups();
    renderLinkPage(linkInAddress());
    return;
  }
  currentHash = hash;
  const [path, queryString = ""] = hash.slice(1).split("?");
  const match = ROUTES.map(([re, load, section, title]) => [re.exec(path), load, section, title]).find(([m]) => m);
  runCleanups();
  const token = ++navToken;
  highlightNav(match ? match[2] : null);
  setTitle(match ? match[3]() : t("Page not found"));
  if (!view) return;
  clear(view).append(loading());
  window.scrollTo(0, 0);
  if (!match) {
    clear(view).append(h("div", { class: "empty" }, h("h3", {}, t("Page not found")),
      h("a", { class: "btn", href: "#/" }, t("Go to the dashboard"))));
    return;
  }
  const [m, load] = match;
  // Each page renders into its own element. A page that is still loading when the user moves on can then only fill
  // its own, already removed element, never the next page.
  const page = h("div", { class: "page" });
  try {
    const module = await load();
    if (token !== navToken) return;
    clear(view).append(page);
    await module.default({
      root: page,
      params: m.slice(1),
      query: new URLSearchParams(queryString),
      cleanup: (fn) => cleanups.push(fn),
      setDirtyCheck: (fn) => { dirtyCheck = fn; },
      isCurrent: () => token === navToken,
      setTitle: (text) => { if (token === navToken && text !== pageTitle) { pageTitle = text; drawTitle(); } },
      setUnread: (count) => { if (token === navToken) { unread = count; drawTitle(); } },
    });
  } catch (err) {
    if (token !== navToken) return;
    if (!(err instanceof ApiError)) console.error(err);  // API errors are shown on the page; this is for real bugs
    clear(view).append(banner("error", icon("alert", 18), h("div", {}, h("strong", {}, t("Something went wrong.")), " ", err.message || String(err)),
      h("button", { class: "btn sm", type: "button", onclick: route }, t("Retry"))));
  }
  if (token === navToken) repaintView();
}

// Safari on iPhones can leave a piece of the page before on screen once the new one is drawn: a tile of the page that
// it does not paint anew (seen going from the dashboard to Sign-ups through the menu). Giving the view a layer of its
// own for a moment makes Safari paint all of it, and the page behind it once the layer goes. Other browsers merely
// paint it once more. Toasts, dialogs and the menus are not inside the view, so the transform moves nothing.
function repaintView() {
  const shown = view;
  if (!shown) return;
  shown.style.transform = "translateZ(0)";
  requestAnimationFrame(() => requestAnimationFrame(() => { shown.style.transform = ""; }));
}

function highlightNav(section) {
  let current = null;
  for (const link of document.querySelectorAll("[data-section]")) {
    if (link.dataset.section === section) {
      link.setAttribute("aria-current", "page");
      current = link;
    } else link.removeAttribute("aria-current");
  }
  // on a phone the menu button names the section the page belongs to
  const label = app.querySelector(".nav-toggle-label");
  if (label) label.textContent = current ? current.title : t("Menu");
  setNavOpen(false);
}

// On a phone the sections are a list that the menu button opens, with their names: a row of icons has no room for all
// of them, and the ones that do not fit would be out of sight.
function setNavOpen(open) {
  const topbar = app.querySelector(".topbar");
  if (!topbar) return;
  topbar.classList.toggle("nav-open", open);
  topbar.querySelector(".nav-toggle")?.setAttribute("aria-expanded", String(open));
}

// ---------------------------------------------------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------------------------------------------------
function navLink(href, section, label, iconName) {
  // The label is hidden on narrow screens; title and aria-label keep the icon understandable.
  return h("a", { href, "data-section": section, class: "nav-link", title: label, "aria-label": label }, icon(iconName, 16), h("span", {}, label));
}

function renderShell() {
  const links = [
    navLink("#/", "home", t("Dashboard"), "home"),
    navLink("#/games", "games", t("Games"), "server"),
    navLink("#/configs", "configs", t("My configs"), "file"),
    navLink("#/signups", "signups", t("Sign-ups"), "calendar"),
    canGenerate() ? navLink("#/generate", "generate", t("Generate"), "zap") : null,
    navLink("#/worlds", "worlds", t("Custom games"), "package"),
    isAdmin() ? navLink("#/admin/users", "admin", t("Admin"), "settings") : null,
  ];
  view = h("main", { id: "view", class: "view", tabindex: "-1" });
  const userButton = h("button", { class: "btn ghost user-btn", type: "button", "aria-haspopup": "menu" },
    userPicture(), h("span", {}, state.user.username), isAdmin() ? h("span", { class: "badge accent" }, t("admin"))
      : state.user.role === "organizer" ? h("span", { class: "badge accent" }, t("organizer")) : null);
  const menu = h("div", { class: "menu", hidden: true, role: "menu" });
  menuKeys(menu);
  userButton.onclick = (e) => {
    e.stopPropagation();
    setNavOpen(false);
    if (menu.hidden) drawUserMenu(menu);
    menu.hidden = !menu.hidden;
  };
  const navToggle = h("button", { class: "btn ghost nav-toggle", type: "button", "aria-expanded": "false",
    "aria-controls": "main-nav" }, icon("menu", 18), h("span", { class: "nav-toggle-label" }, t("Menu")));
  navToggle.onclick = (e) => {
    e.stopPropagation();
    menu.hidden = true;
    setNavOpen(!navToggle.closest(".topbar").classList.contains("nav-open"));
  };
  document.addEventListener("click", () => {
    menu.hidden = true;
    setNavOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    menu.hidden = true;
    setNavOpen(false);
  });
  const topbar = h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/" }, h("img", { class: "brand-mark", src: "favicon.svg", alt: "" }), h("span", { class: "brand-name" }, "Archipelago Manager")),
        navToggle,
        h("nav", { class: "nav", id: "main-nav", "aria-label": t("Main") }, links),
        h("div", { class: "user-menu" }, userButton, menu)));
  clear(app).append(topbar, view, h("footer", { class: "footer muted small" }, `Archipelago Manager ${state.status.version}`));
  fitNav();
  document.fonts?.ready.then(fitNav);
}

// The nav scrolls sideways when its links do not fit, which would hide the last ones (Admin, for administrators).
// Measure instead of guessing a breakpoint: link count and font widths differ.
function fitNav() {
  const topbar = app.querySelector(".topbar");
  const nav = topbar?.querySelector(".nav");
  if (!nav) return;
  topbar.classList.remove("no-brand", "icons-only");
  if (getComputedStyle(topbar.querySelector(".nav-toggle")).display !== "none") return;  // a phone: the menu button's list
  for (const step of ["no-brand", "icons-only"]) {
    if (nav.scrollWidth <= nav.clientWidth) return;
    topbar.classList.add(step);
  }
}
let fitPending = false;
window.addEventListener("resize", () => {
  if (fitPending) return;
  fitPending = true;
  requestAnimationFrame(() => { fitPending = false; fitNav(); });
});

// The picture of the user's linked Discord account, else the outline of a person. It comes from Discord's image server,
// which is not told which page asked; if it cannot be loaded, the outline stands in.
function userPicture() {
  const url = state.user.discord_avatar;
  if (!url) return icon("user", 16);
  const picture = h("img", { class: "avatar", referrerpolicy: "no-referrer", src: url, alt: "", width: 22, height: 22 });
  picture.addEventListener("error", () => picture.replaceWith(icon("user", 16)), { once: true });
  return picture;
}

function drawUserMenu(menu) {
  const user = state.user;
  const item = (iconName, label, onclick) => h("button", { class: "menu-item", type: "button", role: "menuitem",
    onclick: (e) => { e.stopPropagation(); menu.hidden = true; onclick(); } }, icon(iconName, 14), label);
  clear(menu).append(
    item("key", user.has_password ? t("Change password") : t("Set a password"), passwordDialog),
    user.discord_name
      ? [h("div", { class: "menu-note" }, t("Discord: {name}", { name: user.discord_name })), item("link", t("Unlink Discord"), unlinkDiscord)]
      : state.status.discord_login ? item("link", t("Link Discord"), linkDiscord) : null,
    (user.discord_name || state.status.discord_login) && state.status.ping_kinds.length
      ? item("bell", t("Discord mentions…"), pingsDialog) : null,
    item("key", t("API tokens"), () => { location.hash = "#/tokens"; }),
    h("button", { class: "menu-item", type: "button", role: "menuitem", "aria-haspopup": "menu",
      onclick: (e) => { e.stopPropagation(); drawLanguageMenu(menu); } },
      icon("globe", 14), t("Language"), h("span", { class: "menu-value" }, LANGUAGES[lang], icon("chevron", 12))),
    h("button", { class: "menu-item", type: "button", role: "menuitem", "aria-haspopup": "menu",
      onclick: (e) => { e.stopPropagation(); drawThemeMenu(menu); } },
      icon("moon", 14), t("Theme"), h("span", { class: "menu-value" }, THEMES[theme()][1](), icon("chevron", 12))),
    h("div", { class: "menu-sep" }),
    item("logout", t("Sign out"), logout));
}

// ---------------------------------------------------------------------------------------------------------------------
// light or dark: as the system asks (style.css), or as chosen here and kept in this browser (read by theme.js)
// ---------------------------------------------------------------------------------------------------------------------
const THEME_KEY = "apm-theme";
const THEMES = { system: ["monitor", () => t("As on this device")], light: ["sun", () => t("Light")], dark: ["moon", () => t("Dark")] };
const theme = () => document.documentElement.dataset.theme || "system";

function showTheme(choice) {
  if (THEMES[choice] && choice !== "system") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
}

function setTheme(choice) {
  showTheme(choice);
  try {
    if (choice === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, choice);
  } catch { /* private mode: the choice lasts until reload only */ }
}
// the browser's other tabs of this server follow a choice made in one of them
window.addEventListener("storage", (e) => { if (e.key === THEME_KEY || e.key === null) showTheme(e.newValue); });

// The user menu's page of themes; a choice shows at once, and the menu stays open on it
function drawThemeMenu(menu) {
  clear(menu).append(
    h("button", { class: "menu-item menu-back", type: "button", onclick: (e) => { e.stopPropagation(); drawUserMenu(menu); } },
      icon("back", 14), t("Theme")),
    h("div", { class: "menu-sep" }),
    Object.entries(THEMES).map(([key, [iconName, label]]) => h("button", { class: "menu-item", type: "button", role: "menuitemradio",
      "aria-checked": String(key === theme()), onclick: (e) => { e.stopPropagation(); setTheme(key); drawThemeMenu(menu); } },
      icon(iconName, 14), h("span", { class: "grow" }, label()), key === theme() ? icon("check", 14) : null)));
  menu.querySelector("[aria-checked=true]")?.focus();
}

// ---------------------------------------------------------------------------------------------------------------------
// language
// ---------------------------------------------------------------------------------------------------------------------
// Every language by its own name, with its code and, below, its name in the current language (Deutsch · German)
function languageOptions() {
  let names = null;
  try { names = new Intl.DisplayNames([locale], { type: "language" }); } catch { /* an older browser: own names only */ }
  return Object.entries(LANGUAGES).map(([code, own]) => {
    const here = names?.of(code);
    return h("button", { class: "menu-item lang-option", type: "button", role: "menuitemradio", "aria-checked": String(code === lang),
      onclick: (e) => { e.stopPropagation(); switchLanguage(code); } },
      h("span", { class: "lang-code", "aria-hidden": "true" }, code.toUpperCase()),
      h("span", { class: "lang-names" }, h("span", { lang: code }, own),
        here && here.toLowerCase() !== own.toLowerCase() ? h("span", { class: "muted small" }, here) : null),
      code === lang ? icon("check", 14) : null);
  });
}

function switchLanguage(code) {
  if (code === lang) return;
  if (dirtyCheck?.() && !confirm(t("You have unsaved changes. Leave this page anyway?"))) return;
  setLanguage(code);
}

// Up and down move between the entries of a menu
function menuKeys(menu) {
  menu.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = [...menu.querySelectorAll(".menu-item")];
    const at = items.indexOf(document.activeElement);
    items[(at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
    e.preventDefault();
  });
}

// The user menu's second page: the languages, and the way back to the first
function drawLanguageMenu(menu) {
  clear(menu).append(
    h("button", { class: "menu-item menu-back", type: "button", onclick: (e) => { e.stopPropagation(); drawUserMenu(menu); } },
      icon("back", 14), t("Language")),
    h("div", { class: "menu-sep" }),
    languageOptions());
  menu.querySelector("[aria-checked=true]")?.focus();
}

// On the sign-in page: the current language, which opens the list of all of them
function languagePicker() {
  const list = h("div", { class: "menu lang-menu", role: "menu", hidden: true }, languageOptions());
  const button = h("button", { class: "btn ghost sm", type: "button", "aria-haspopup": "menu", "aria-expanded": "false" },
    icon("globe", 14), LANGUAGES[lang], icon("expand", 12));
  const close = () => {
    list.hidden = true;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", close);
  };
  button.onclick = (e) => {
    e.stopPropagation();
    if (!list.hidden) { close(); return; }
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
    document.addEventListener("click", close);
    list.querySelector("[aria-checked=true]")?.focus();
  };
  menuKeys(list);
  list.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); button.focus(); }
  });
  return h("div", { class: "lang-picker" }, button, list);
}

// Discord sends the browser back to the configured address of this web UI, and the sign-in only finishes in the browser
// (and on the address) that started it. Starting on another address would end with "expired", so say so up front.
function onSiteAddress() {
  const site = state.status.site_url;
  try { return !site || new URL(site).origin === location.origin; } catch { return false; }
}

function openOnSiteAddress(why) {
  const site = state.status.site_url;
  const modal = openModal({
    title: t("Open the right address"),
    body: h("div", { class: "stack" }, h("p", {}, why),
      h("p", {}, h("a", { href: `${site}/${location.hash}` }, site))),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Close"))],
  });
}

async function linkDiscord() {
  if (!onSiteAddress()) {
    openOnSiteAddress(t("Discord sends you back to the address this server is set up with, so link your account from there:"));
    return;
  }
  try {
    const { url } = await post("/api/auth/discord/link");
    location.href = url;
  } catch (err) {
    toast(err.message, "error");
  }
}

async function unlinkDiscord() {
  if (!state.user.has_password) {
    toast(t("Set a password first, or you could not sign in any more."), "error");
    passwordDialog();
    return;
  }
  if (!(await confirmDialog(t("You will no longer be able to sign in with this Discord account. You can link it again later."),
    { title: t("Unlink Discord?"), confirmText: t("Unlink") }))) return;
  try {
    state.user = await post("/api/auth/discord/unlink");
    app.querySelector(".user-btn .avatar")?.replaceWith(userPicture());
    toast(t("Discord unlinked"), "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

// What the server's Discord channel can mention a player for (notify.PING_KINDS)
const PING_KINDS = [
  ["game_ready", () => t("A new game you play in is ready")],
  ["game_online", () => t("A game you play in is online")],
  ["hint_found", () => t("Someone found an item that was hinted for you")],
  ["player_stuck", () => t("Someone in a game you play in is stuck")],
  ["player_inactive", () => t("You have not played a game for a while and the others play on")],
  ["slot_handover", () => t("A slot is offered to you, or looks for a new player")],
  ["signup_open", () => t("Sign-ups open")],
  ["signup_closing", () => t("Sign-ups close soon and you have not entered yet")],
];

function pingsDialog() {
  const linked = !!state.user.discord_name;
  const offered = state.status.ping_kinds;
  const boxes = PING_KINDS.map(([kind, label]) => {
    const box = checkbox(label(), state.user.discord_pings.includes(kind),
      offered.includes(kind) ? null : t("This server does not send these messages at the moment."));
    box.input.disabled = !linked;
    box.kind = kind;
    return box;
  });
  const save = h("button", { class: "btn primary", type: "button", disabled: !linked }, t("Save"));
  save.onclick = action(async () => {
    state.user = await put("/api/auth/discord/pings", { pings: boxes.filter((b) => b.input.checked).map((b) => b.kind) });
    modal.close();
    toast(t("Saved"), "success");
  });
  const modal = openModal({
    title: t("Discord mentions"),
    body: h("div", { class: "stack" },
      h("p", { class: "muted" }, t("Notifications on this server's Discord channel can mention you when they concern you, so Discord lets you know. Choose when:")),
      linked ? null : banner("info", icon("link", 16), h("div", { class: "grow stack tight" },
        h("span", {}, t("Link your Discord account first, so the messages know whom to mention.")),
        state.status.discord_login ? h("div", {}, h("button", { class: "btn sm", type: "button",
          onclick: () => { modal.close(); linkDiscord(); } }, t("Link Discord"))) : null)),
      h("div", { class: "stack tight", role: "group", "aria-label": t("Mention me when") }, boxes)),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save],
  });
}

async function logout() {
  await api("POST", "/api/auth/logout", {}).catch(() => {});
  state.user = null;
  await boot();
}

function passwordDialog() {
  // Accounts created through Discord have no password yet; they set a first one without a current one.
  const first = !state.user.has_password;
  const current = first ? null : h("input", { type: "password", class: "input", autocomplete: "current-password" });
  const next = h("input", { type: "password", class: "input", autocomplete: "new-password", minLength: 8 });
  const again = h("input", { type: "password", class: "input", autocomplete: "new-password" });
  // on by default: whoever knew the old password may have made tokens with it
  const revoke = checkbox(t("Also revoke my API tokens"), true, t("Scripts and bots then need new ones."));
  const title = first ? t("Set a password") : t("Change password");
  const save = h("button", { class: "btn primary", type: "button" }, title);
  save.onclick = action(async () => {
    if (next.value !== again.value) throw new Error(t("The new passwords do not match"));
    const r = await post("/api/auth/password", { current_password: current ? current.value : "", new_password: next.value,
      revoke_tokens: revoke.input.checked });
    modal.close();
    if (first) state.user = { ...state.user, has_password: true };
    const revoked = r.tokens_revoked ? ` ${plural(r.tokens_revoked, "{n} API token was revoked.", "{n} API tokens were revoked.")}` : "";
    toast((first ? t("Password set. You can now also sign in with {name} and this password.", { name: state.user.username })
      : t("Password changed")) + revoked, "success");
  });
  const modal = openModal({
    title,
    body: h("div", { class: "stack" }, current ? field(t("Current password"), current) : null,
      field(t("New password"), next, t("At least 8 characters")), field(t("Repeat new password"), again), revoke),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, t("Cancel")), save],
  });
}

// ---------------------------------------------------------------------------------------------------------------------
// what Discord sent the browser back with: #/?discord=linked or #/?discord_error=expired (fixed codes only)
// ---------------------------------------------------------------------------------------------------------------------
const DISCORD_ERRORS = {
  expired: () => t("The sign-in with Discord took too long or was started in another browser. Please try again."),
  denied: () => t("Signing in with Discord was cancelled."),
  not_linked: () => t("This Discord account is not linked to an account here. Sign in with your username and password and link Discord in the user menu, or ask an administrator."),
  disabled_user: () => t("This account is disabled. Ask an administrator."),
  not_member: () => t("Only members of this server's Discord community can sign in with Discord."),
  taken: () => t("This Discord account is already linked to another user."),
  disabled: () => t("Signing in with Discord is not set up on this server."),
  invite: () => t("This invite link cannot be used any more: it expired, was used up or was revoked. Ask whoever sent it for a new one."),
  discord: () => t("Discord did not accept the sign-in. Try again; if it keeps happening, an administrator should check the client ID and secret."),
  unreachable: () => t("Discord could not be reached. Try again in a moment."),
  busy: () => t("Too many sign-in attempts. Try again in a few minutes."),
};
const DISCORD_DONE = {
  linked: () => t("Your Discord account is linked. You can now sign in with Discord."),
  welcome: () => t("Welcome! Your account was created with Discord. Set a password in the user menu if you also want to sign in without Discord."),
};

function takeDiscordResult() {
  const [path, query = ""] = (location.hash || "#/").slice(1).split("?");
  const params = new URLSearchParams(query);
  const error = params.get("discord_error"), done = params.get("discord");
  if (!error && !done) return null;
  params.delete("discord_error");
  params.delete("discord");
  const rest = params.toString();
  history.replaceState(null, "", `#${path}${rest ? `?${rest}` : ""}`);
  if (error) return { kind: "error", text: (DISCORD_ERRORS[error] || DISCORD_ERRORS.discord)() };
  return DISCORD_DONE[done] ? { kind: "success", text: DISCORD_DONE[done]() } : null;
}

// ---------------------------------------------------------------------------------------------------------------------
// sign in / first-run setup / registration
// ---------------------------------------------------------------------------------------------------------------------
function renderAuth(notice) {
  view = null;
  setTitle("");
  const status = state.status;
  let mode = status.needs_setup ? "setup" : "login";
  const card = h("div", { class: "auth-card card" });
  const draw = () => {
    // the first administrator needs the code the container writes to its log (routes/auth.py)
    const code = mode === "setup" ? h("input", { class: "input", autocomplete: "off", autocapitalize: "characters",
      required: true, autofocus: true, placeholder: "XXXX-XXXX-XXXX" }) : null;
    const username = h("input", { class: "input", autocomplete: "username", required: true, autofocus: !code });
    const password = h("input", { class: "input", type: "password", required: true,
      autocomplete: mode === "login" ? "current-password" : "new-password", minLength: mode === "login" ? 1 : 8 });
    const confirm = mode === "login" ? null : h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
    const error = h("div", { class: "form-error", role: "alert" }, notice?.kind === "error" ? notice.text : null);
    notice = null;
    const submit = h("button", { class: "btn primary block", type: "submit" },
      { setup: t("Create administrator"), login: t("Sign in"), register: t("Create account") }[mode]);
    const form = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      error.textContent = "";
      if (confirm && confirm.value !== password.value) { error.textContent = t("The passwords do not match"); return; }
      submit.disabled = true;
      try {
        const path = { setup: "setup", login: "login", register: "register" }[mode];
        await post(`/api/auth/${path}`, { username: username.value, password: password.value, ...(code ? { code: code.value } : {}) });
        await boot();
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    } },
    code ? field(t("Setup code"), code, t("It is in the container's log (on Unraid: the container's Logs), and in the file setup-code.txt in the data folder.")) : null,
    field(t("Username"), username),
    field(t("Password"), password, mode === "login" ? null : t("At least 8 characters")),
    confirm ? field(t("Repeat password"), confirm) : null,
    error, submit);
    clear(card).append(
      authBrand(),
      mode === "setup" ? h("p", { class: "muted" }, t("Welcome! Create the administrator account to get started.")) : null,
      form,
      mode !== "setup" && status.discord_login ? [h("div", { class: "auth-or" }, t("or")), discordButton(status)] : null,
      mode !== "setup" && status.allow_registration
        ? h("p", { class: "muted center small" }, mode === "login" ? t("No account yet?") : t("Already registered?"), " ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); mode = mode === "login" ? "register" : "login"; draw(); } },
            mode === "login" ? t("Create one") : t("Sign in")))
        : null,
      languagePicker());
  };
  draw();
  clear(app).append(h("div", { class: "auth-wrap" }, card));
}

// A plain link: the browser goes to this server, which sends it on to Discord. It starts on the configured address of
// this web UI, because that is where Discord sends it back.
function discordButton(status) {
  const next = /^#\/[A-Za-z0-9/_-]*$/.test(location.hash) ? location.hash : "";
  const href = `${status.site_url}/api/auth/discord/login${next ? `?next=${encodeURIComponent(next)}` : ""}`;
  return h("a", { class: "btn discord block", href }, icon("login", 16), t("Sign in with Discord"));
}

// ---------------------------------------------------------------------------------------------------------------------
// invite and reset links (#/invite/<token>, #/reset/<token>): an administrator hands them out (views/admin-users.js).
// The token stays in the fragment, which the browser never sends to the server; the page posts it.
// ---------------------------------------------------------------------------------------------------------------------
function linkInAddress() {
  const match = /^#\/(invite|reset)\/([A-Za-z0-9_-]{20,128})$/.exec(location.hash);
  return match ? { kind: match[1], token: match[2] } : null;
}

// Done with the link: out of the address and the history, on to the app
async function leaveLink(notice) {
  history.replaceState(null, "", "#/");
  await boot();
  if (notice) toast(notice, "success", 9000);
}

function authBrand() {
  return h("div", { class: "auth-brand" }, h("img", { class: "brand-mark large", src: "favicon.svg", alt: "" }), h("h1", {}, "Archipelago Manager"));
}

async function renderLinkPage({ kind, token }) {
  view = null;
  setTitle(kind === "invite" ? t("Invitation") : t("New password"));
  const card = h("div", { class: "auth-card card" }, loading());
  clear(app).append(h("div", { class: "auth-wrap" }, card));
  let info;
  try {
    info = await post(`/api/auth/${kind}`, { token });
  } catch (err) {
    clear(card).append(authBrand(), banner("error", icon("alert", 16), h("div", { class: "grow" }, err.message)),
      h("p", { class: "center small" }, h("a", { href: "#/", onclick: (e) => { e.preventDefault(); leaveLink(); } },
        state.user ? t("Go to the dashboard") : t("Go to the sign-in page"))),
      languagePicker());
    return;
  }
  // someone signed in who opens the link ends up signed in as its account
  const signedIn = state.user && !(kind === "reset" && state.user.username === info.username)
    ? banner("info", icon("user", 16), h("div", { class: "grow small" }, t("You are signed in as {name}. Going on signs you in as the account this link is for instead.", { name: state.user.username }))) : null;
  const password = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true, minLength: 8 });
  const again = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
  const error = h("div", { class: "form-error", role: "alert" });
  const checked = () => {
    if (password.value !== again.value) throw new Error(t("The passwords do not match"));
  };
  if (kind === "reset") {
    const submit = h("button", { class: "btn primary block", type: "submit" }, t("Set password"));
    const form = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      error.textContent = "";
      submit.disabled = true;
      try {
        checked();
        const result = await post("/api/auth/reset/use", { token, password: password.value });
        const revoked = result.tokens_revoked ? ` ${plural(result.tokens_revoked, "{n} API token was revoked.", "{n} API tokens were revoked.")}` : "";
        await leaveLink(t("Your new password is set. You were signed out everywhere else.") + revoked);
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    } },
    // the name for the browser's password manager, which saves the new password with it
    h("input", { type: "text", autocomplete: "username", value: info.username, hidden: true, readOnly: true }),
    field(t("New password"), password, t("At least 8 characters")), field(t("Repeat new password"), again), error, submit);
    clear(card).append(authBrand(), h("p", {}, t("Set a new password for {name}.", { name: info.username })), signedIn, form, languagePicker());
    password.focus();
    return;
  }

  const username = h("input", { class: "input", autocomplete: "username", required: true });
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("Create account"));
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    try {
      checked();
      await post("/api/auth/invite/join", { token, username: username.value, password: password.value });
      await leaveLink(t("Welcome! Your account is ready."));
    } catch (err) {
      error.textContent = err.message;
      submit.disabled = false;
    }
  } },
  field(t("Username"), username), field(t("Password"), password, t("At least 8 characters")), field(t("Repeat password"), again), error, submit);
  // with Discord: the invite goes along through Discord's sign-in, which comes back on the server's own address
  const discord = info.discord ? h("button", { class: "btn discord block", type: "button", onclick: action(async () => {
    if (!onSiteAddress()) {
      openOnSiteAddress(t("Discord sends you back to the address this server is set up with, so open the invite there:"));
      return;
    }
    location.href = (await post("/api/auth/invite/discord", { token })).url;
  }) }, icon("login", 16), t("Sign up with Discord")) : null;
  clear(card).append(authBrand(),
    h("p", {}, info.invited_by ? t("{name} invites you to this Archipelago server.", { name: info.invited_by }) : t("You are invited to this Archipelago server."),
      info.role === "organizer" ? ` ${t("Your account will be an organizer's: you add games, generate them and run sign-up rounds.")}` : ""),
    info.note ? h("p", { class: "muted small" }, `“${info.note}”`) : null,
    signedIn, form, discord ? [h("div", { class: "auth-or" }, t("or")), discord] : null,
    h("p", { class: "muted center small" }, t("The invitation is valid until {date}.", { date: fmtDate(info.expires_at) })),
    languagePicker());
  username.focus();
}

// ---------------------------------------------------------------------------------------------------------------------
export async function boot() {
  const discordResult = takeDiscordResult();
  try {
    await refreshStatus();
  } catch (err) {
    clear(app).append(h("div", { class: "auth-wrap" }, banner("error", t("Cannot reach the server: {error}", { error: err.message }))));
    return;
  }
  if (linkInAddress()) {
    await renderLinkPage(linkInAddress());
    return;
  }
  if (!state.user) {
    renderAuth(discordResult);
    return;
  }
  if (discordResult) toast(discordResult.text, discordResult.kind, 9000);
  renderShell();
  currentHash = null;
  await route();
}

setUnauthorizedHandler(() => { state.user = null; boot(); });
window.addEventListener("hashchange", route);
boot();
