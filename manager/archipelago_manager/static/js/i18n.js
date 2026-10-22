// The web UI in English or German. English text is the key: t("Save") shows "Speichern" in German, and any text the
// dictionary does not have yet stays English instead of breaking the page.
//
// - t(text, { name }) fills in {name}; plural(n, one, other, vars) first picks the form for n (and fills in {n}).
// - The server answers in English. tServer() turns its messages into the chosen language: exact texts from
//   i18n/de.json's "server" part, and messages with changing parts ("Game {0} not found") by their pattern.

export const LANGUAGES = { en: "English", de: "Deutsch" };
const STORAGE_KEY = "apm-language";

function stored() {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

export const lang = LANGUAGES[stored()] ? stored() : (navigator.language || "").toLowerCase().startsWith("de") ? "de" : "en";
document.documentElement.lang = lang;

// For dates and numbers: German formats in German; in English the browser's own English variant, else plain "en"
export const locale = lang === "de" ? "de-DE" : (navigator.language || "").toLowerCase().startsWith("en") ? navigator.language : "en";

let strings = {}, server = {};
const patterns = [];
if (lang !== "en") {
  try {
    const response = await fetch(new URL(`./i18n/${lang}.json`, import.meta.url), { cache: "no-cache" });
    if (response.ok) ({ strings = {}, server = {} } = await response.json());
  } catch (err) {
    console.error("The translation could not be loaded", err);
  }
  for (const [key, value] of Object.entries(server)) {
    if (!/\{\d+\}/.test(key)) continue;
    const parts = key.split(/\{(\d+)\}/);  // text, index, text, index, ..., text
    const order = parts.filter((_, i) => i % 2).map(Number);
    const source = parts.map((part, i) => (i % 2 ? "([\\s\\S]+?)" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))).join("");
    patterns.push({ re: new RegExp(`^${source}$`), order, value });
  }
}

function fill(text, vars) {
  return vars ? text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match)) : text;
}

export function t(text, vars) {
  return fill(strings[text] ?? text, vars);
}

export function plural(n, one, other, vars) {
  return t(n === 1 ? one : other, { n, ...vars });
}

export function tServer(message, depth = 0) {
  if (lang === "en" || typeof message !== "string") return message;
  if (message in server) return server[message];
  if (message in strings) return strings[message];
  for (const { re, order, value } of patterns) {
    const match = re.exec(message);
    if (!match) continue;
    const values = {};
    // the changing parts may be English messages themselves ("Could not start: {0}")
    order.forEach((index, i) => { values[index] = depth < 2 ? tServer(match[i + 1], depth + 1) : match[i + 1]; });
    return value.replace(/\{(\d+)\}/g, (m, index) => (index in values ? values[index] : m));
  }
  return message;
}

export function setLanguage(code) {
  if (!LANGUAGES[code] || code === lang) return;
  try { localStorage.setItem(STORAGE_KEY, code); } catch { /* private mode: the choice lasts until reload only */ }
  location.reload();
}
