import { h, clear, icon, toast, openModal, action } from "./util.js";
import { api, state, refreshStatus, setUnauthorizedHandler, isAdmin, post, ApiError } from "./api.js";
import { banner, loading, field } from "./components.js";

const ROUTES = [
  [/^\/?$/, () => import("./views/dashboard.js"), "home"],
  [/^\/games$/, () => import("./views/games.js"), "games"],
  [/^\/games\/(\d+)$/, () => import("./views/game.js"), "games"],
  [/^\/configs$/, () => import("./views/configs.js"), "configs"],
  [/^\/configs\/new$/, () => import("./views/config-edit.js"), "configs"],
  [/^\/configs\/(\d+)$/, () => import("./views/config-edit.js"), "configs"],
  [/^\/generate$/, () => import("./views/generate.js"), "generate"],
  [/^\/jobs\/(\d+)$/, () => import("./views/job.js"), "generate"],
  [/^\/signups$/, () => import("./views/signups.js"), "signups"],
  [/^\/signups\/(\d+)$/, () => import("./views/signup.js"), "signups"],
  [/^\/worlds$/, () => import("./views/worlds.js"), "worlds"],
  [/^\/admin\/users$/, () => import("./views/admin-users.js"), "admin"],
  [/^\/admin\/settings$/, () => import("./views/admin-settings.js"), "admin"],
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

async function route() {
  const hash = location.hash || "#/";
  if (dirtyCheck?.() && currentHash !== null && hash !== currentHash) {
    if (!confirm("You have unsaved changes. Leave this page anyway?")) {
      history.replaceState(null, "", currentHash);
      return;
    }
  }
  currentHash = hash;
  const [path, queryString = ""] = hash.slice(1).split("?");
  const match = ROUTES.map(([re, load, section]) => [re.exec(path), load, section]).find(([m]) => m);
  runCleanups();
  const token = ++navToken;
  highlightNav(match ? match[2] : null);
  if (!view) return;
  clear(view).append(loading());
  window.scrollTo(0, 0);
  if (!match) {
    clear(view).append(h("div", { class: "empty" }, h("h3", {}, "Page not found"),
      h("a", { class: "btn", href: "#/" }, "Go to the dashboard")));
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
    });
  } catch (err) {
    if (token !== navToken) return;
    if (!(err instanceof ApiError)) console.error(err);  // API errors are shown on the page; this is for real bugs
    clear(view).append(banner("error", icon("alert", 18), h("div", {}, h("strong", {}, "Something went wrong. "), err.message || String(err)),
      h("button", { class: "btn sm", type: "button", onclick: route }, "Retry")));
  }
}

function highlightNav(section) {
  for (const link of document.querySelectorAll("[data-section]")) {
    if (link.dataset.section === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

// ---------------------------------------------------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------------------------------------------------
function navLink(href, section, label, iconName) {
  // The label is hidden on narrow screens; title and aria-label keep the icon understandable.
  return h("a", { href, "data-section": section, class: "nav-link", title: label, "aria-label": label }, icon(iconName, 16), h("span", {}, label));
}

function renderShell() {
  const perms = state.status.permissions;
  const links = [
    navLink("#/", "home", "Dashboard", "home"),
    navLink("#/games", "games", "Games", "server"),
    navLink("#/configs", "configs", "My configs", "file"),
    navLink("#/signups", "signups", "Sign-ups", "calendar"),
    (isAdmin() || perms.generate) ? navLink("#/generate", "generate", "Generate", "zap") : null,
    navLink("#/worlds", "worlds", "Custom games", "package"),
    isAdmin() ? navLink("#/admin/users", "admin", "Admin", "settings") : null,
  ];
  view = h("main", { id: "view", class: "view", tabindex: "-1" });
  const userButton = h("button", { class: "btn ghost user-btn", type: "button", "aria-haspopup": "menu" },
    icon("user", 16), h("span", {}, state.user.username), isAdmin() ? h("span", { class: "badge accent" }, "admin") : null);
  const menu = h("div", { class: "menu", hidden: true, role: "menu" },
    h("button", { class: "menu-item", type: "button", role: "menuitem", onclick: () => { menu.hidden = true; passwordDialog(); } },
      icon("key", 14), "Change password"),
    h("button", { class: "menu-item", type: "button", role: "menuitem", onclick: logout }, icon("logout", 14), "Sign out"));
  userButton.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  document.addEventListener("click", () => { menu.hidden = true; });
  const topbar = h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/" }, h("span", { class: "brand-mark" }, "AP"), h("span", { class: "brand-name" }, "Archipelago Manager")),
        h("nav", { class: "nav", "aria-label": "Main" }, links),
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

async function logout() {
  await api("POST", "/api/auth/logout", {}).catch(() => {});
  state.user = null;
  await boot();
}

function passwordDialog() {
  const current = h("input", { type: "password", class: "input", autocomplete: "current-password" });
  const next = h("input", { type: "password", class: "input", autocomplete: "new-password", minLength: 8 });
  const again = h("input", { type: "password", class: "input", autocomplete: "new-password" });
  const save = h("button", { class: "btn primary", type: "button" }, "Change password");
  save.onclick = action(async () => {
    if (next.value !== again.value) throw new Error("The new passwords do not match");
    await post("/api/auth/password", { current_password: current.value, new_password: next.value });
    modal.close();
    toast("Password changed", "success");
  });
  const modal = openModal({
    title: "Change password",
    body: h("div", { class: "stack" }, field("Current password", current), field("New password", next, "At least 8 characters"),
      field("Repeat new password", again)),
    actions: [h("button", { class: "btn", type: "button", onclick: () => modal.close() }, "Cancel"), save],
  });
}

// ---------------------------------------------------------------------------------------------------------------------
// sign in / first-run setup / registration
// ---------------------------------------------------------------------------------------------------------------------
function renderAuth() {
  view = null;
  const status = state.status;
  let mode = status.needs_setup ? "setup" : "login";
  const card = h("div", { class: "auth-card card" });
  const draw = () => {
    const username = h("input", { class: "input", autocomplete: "username", required: true, autofocus: true });
    const password = h("input", { class: "input", type: "password", required: true,
      autocomplete: mode === "login" ? "current-password" : "new-password", minLength: mode === "login" ? 1 : 8 });
    const confirm = mode === "login" ? null : h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
    const error = h("div", { class: "form-error", role: "alert" });
    const submit = h("button", { class: "btn primary block", type: "submit" },
      { setup: "Create administrator", login: "Sign in", register: "Create account" }[mode]);
    const form = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      error.textContent = "";
      if (confirm && confirm.value !== password.value) { error.textContent = "The passwords do not match"; return; }
      submit.disabled = true;
      try {
        const path = { setup: "setup", login: "login", register: "register" }[mode];
        await post(`/api/auth/${path}`, { username: username.value, password: password.value });
        await boot();
      } catch (err) {
        error.textContent = err.message;
        submit.disabled = false;
      }
    } },
    field("Username", username),
    field("Password", password, mode === "login" ? null : "At least 8 characters"),
    confirm ? field("Repeat password", confirm) : null,
    error, submit);
    clear(card).append(
      h("div", { class: "auth-brand" }, h("span", { class: "brand-mark large" }, "AP"), h("h1", {}, "Archipelago Manager")),
      mode === "setup" ? h("p", { class: "muted" }, "Welcome! Create the administrator account to get started.") : null,
      form,
      mode !== "setup" && status.allow_registration
        ? h("p", { class: "muted center small" }, mode === "login" ? "No account yet? " : "Already registered? ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); mode = mode === "login" ? "register" : "login"; draw(); } },
            mode === "login" ? "Create one" : "Sign in"))
        : null);
  };
  draw();
  clear(app).append(h("div", { class: "auth-wrap" }, card));
}

// ---------------------------------------------------------------------------------------------------------------------
export async function boot() {
  try {
    await refreshStatus();
  } catch (err) {
    clear(app).append(h("div", { class: "auth-wrap" }, banner("error", `Cannot reach the server: ${err.message}`)));
    return;
  }
  if (!state.user) {
    renderAuth();
    return;
  }
  renderShell();
  currentHash = null;
  await route();
}

setUnauthorizedHandler(() => { state.user = null; boot(); });
window.addEventListener("hashchange", route);
boot();
