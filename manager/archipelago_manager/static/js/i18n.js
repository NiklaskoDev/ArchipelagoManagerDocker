// The web UI in English, German, French or Dutch. English text is the key: t("Save") shows "Speichern" in German, and
// any text a dictionary does not have yet stays English instead of breaking the page.
//
// - t(text, { name }) fills in {name}; plural(n, one, other, vars) first picks the form for n (and fills in {n}).
// - The server answers in English. tServer() turns its messages into the chosen language: exact texts from the
//   "server" part of i18n/<language>.json, and messages with changing parts ("Game {0} not found") by their pattern.

// Each language by its own name
export const LANGUAGES = { en: "English", de: "Deutsch", fr: "Français", nl: "Nederlands" };
// Dates and numbers when the browser does not prefer a variant of the language (en-GB, fr-CA, nl-BE, ...)
const LOCALES = { en: "en", de: "de-DE", fr: "fr-FR", nl: "nl-NL" };
const STORAGE_KEY = "apm-language";

function stored() {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

// The languages the browser asks for, best first. Some systems report tags Intl refuses ("en-US@posix"), which would
// break every date on the page, so those are left out.
function browserLocales() {
  const tags = navigator.languages?.length ? navigator.languages : [navigator.language || ""];
  return tags.flatMap((tag) => {
    try { return Intl.getCanonicalLocales(tag); } catch { return []; }
  });
}

export const lang = LANGUAGES[stored()] ? stored()
  : browserLocales().map((tag) => tag.split("-")[0].toLowerCase()).find((code) => LANGUAGES[code]) || "en";
document.documentElement.lang = lang;
export const locale = browserLocales().find((tag) => tag.split("-")[0].toLowerCase() === lang) || LOCALES[lang];
const pluralRules = new Intl.PluralRules(locale);

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

// A value that changes the direction of the text (a player name with such characters, say) is kept to itself, so it
// cannot turn the rest of the sentence around. Other values go in as they are.
const DIRECTION_CHANGES = /[\u202a-\u202e\u2066-\u2069]/;
const isolated = (value) => (DIRECTION_CHANGES.test(value) ? `\u2068${value}\u2069` : value);

function fill(text, vars) {
  return vars ? text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? isolated(String(vars[name])) : match)) : text;
}

export function t(text, vars) {
  return fill(strings[text] ?? text, vars);
}

// The singular is for whatever the language counts as one: 1 in English, German and Dutch, 0 and 1 in French
export function plural(n, one, other, vars) {
  return t(pluralRules.select(n) === "one" ? one : other, { n, ...vars });
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
