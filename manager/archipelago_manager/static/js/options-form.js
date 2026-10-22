// The options form of the config editor: a field for every option of a game, the way archipelago.gg's player options
// page shows them. The server describes the options (ap_helper.py, option_form) and turns the form's values into the
// YAML (yamlform.py), so this file only draws fields and reads them back.
import { h, clear, icon, withNodes, confirmDialog } from "./util.js";
import { get } from "./api.js";
import { field, banner, select } from "./components.js";
import { t, plural } from "./i18n.js";

const schemas = new Map();

// A game's options. Kept for the session: a big game's item and location names are a few hundred kilobytes.
export function loadSchema(game) {
  if (!schemas.has(game)) {
    schemas.set(game, get(`/api/templates/options?game=${encodeURIComponent(game)}`).catch((err) => {
      schemas.delete(game);
      throw err;
    }));
  }
  return schemas.get(game);
}

// `state` is what /api/configs/form/read answers: name, description, and per option {value}, {random} or {kept}.
export function optionsForm(schema, state, { onChange }) {
  const lists = new Map();  // shared <datalist>s of item and location names, one per kind
  const ctx = { schema, lists, changed: () => { counts(); onChange(); } };

  const nameInput = h("input", { class: "input", value: state.name, maxLength: 16, autocomplete: "off", required: true,
    oninput: ctx.changed });
  const descriptionInput = h("input", { class: "input", value: state.description, maxLength: 500, oninput: ctx.changed });

  const groups = schema.groups.map((group) => {
    const rows = group.options.map((f) => optionRow(f, state.options[f.key], ctx));
    const count = h("span", { class: "badge accent", hidden: true });
    const details = h("details", { class: "card opt-group", open: !group.collapsed },
      h("summary", {}, h("span", { class: "grow" }, group.name), count),
      h("div", { class: "opt-list" }, rows.map((row) => row.node)));
    return { rows, count, details };
  });
  const rows = groups.flatMap((group) => group.rows);

  function counts() {
    for (const group of groups) {
      const n = group.rows.filter((row) => row.changed()).length;
      group.count.hidden = !n;
      group.count.textContent = plural(n, "{n} changed", "{n} changed");
    }
  }
  counts();

  const filter = h("input", { class: "input", type: "search", placeholder: t("Find an option…"), "aria-label": t("Find an option") });
  filter.oninput = () => {
    const q = filter.value.trim().toLowerCase();
    for (const group of groups) {
      let any = false;
      for (const row of group.rows) {
        const show = !q || row.text.includes(q);
        row.node.hidden = !show;
        any ||= show;
      }
      group.details.hidden = !any;
      if (q && any) group.details.open = true;
    }
  };

  const presetNames = Object.keys(schema.presets || {});
  let presetSelect = null;
  if (presetNames.length) {
    presetSelect = select([["", t("Choose a preset…")], ...presetNames.map((name) => [name, name]), ["__defaults__", t("Defaults")]], "");
    presetSelect.onchange = async () => {
      const name = presetSelect.value;
      presetSelect.value = "";
      if (!name) return;
      const label = name === "__defaults__" ? t("Defaults") : name;
      if (!(await confirmDialog(t("Every option is set as in “{preset}”. What you changed here is replaced.", { preset: label }),
        { title: t("Use this preset?"), confirmText: t("Use preset") }))) return;
      const preset = name === "__defaults__" ? {} : schema.presets[name];
      for (const row of rows) row.apply(row.key in preset ? preset[row.key] : row.defaultValue);
      ctx.changed();
    };
  }

  const others = state.others?.length
    ? banner("info", icon("file", 16), h("div", {}, t("This file also sets {list}. The form keeps them as they are; change them in the YAML tab.",
      { list: state.others.join(", ") })))
    : null;

  const node = h("div", { class: "stack" },
    h("div", { class: "card stack" },
      h("div", { class: "form-grid" },
        field(t("Player name"), nameInput, t("Your slot name in the game, at most 16 characters.")),
        field(t("Description"), descriptionInput, t("Optional. Helps you tell your configs apart.")),
        presetSelect ? field(t("Preset"), presetSelect, t("Option sets the game's authors suggest.")) : null),
      filter),
    others,
    groups.map((group) => group.details),
    [...lists.values()]);

  return {
    node,
    game: schema.game,
    // The form's values for /api/configs/form/write. Options it keeps as the YAML has them are left out.
    read() {
      const options = {};
      for (const row of rows) {
        const value = row.read();
        if (value !== undefined) options[row.key] = value;
      }
      return { name: nameInput.value, description: descriptionInput.value, options };
    },
  };
}

// ---------------------------------------------------------------------------------------------------------------------
// one option
// ---------------------------------------------------------------------------------------------------------------------
function optionRow(f, initial, ctx) {
  const id = `opt-${f.key}`;
  const control = makeControl(f, id, ctx);
  // A value to show when the option is random: the default, or the first choice if the default is random itself
  const concrete = f.default !== "random" ? f.default : f.type === "range" ? f.min : f.choices?.[0]?.[0] ?? f.default;
  let kept = initial && "kept" in initial ? initial.kept : null;

  const random = f.random ? h("input", { type: "checkbox" }) : null;
  const randomLabel = random ? h("label", { class: "check opt-random", title: t("Archipelago picks one at random when it generates") },
    random, h("span", {}, t("Random"))) : null;
  const inputs = h("div", { class: "opt-controls" }, control.node, randomLabel);
  const keptNote = h("div", { class: "opt-kept" });
  const reset = h("button", { class: "btn ghost sm icon-only", type: "button", title: t("Back to the default"),
    "aria-label": t("Back to the default") }, icon("restart", 13));

  const current = () => (random?.checked ? "random" : control.get());
  const sync = () => {
    control.disable(!!random?.checked);
    keptNote.hidden = kept === null;
    inputs.hidden = kept !== null;
    reset.hidden = !row.changed();
  };
  const setValue = (value) => {
    kept = null;
    const isRandom = value === "random" && !!random;
    if (random) random.checked = isRandom;
    control.set(isRandom ? concrete : value);
    sync();
  };

  const row = {
    key: f.key,
    defaultValue: f.default,
    text: `${f.label} ${f.key} ${f.doc}`.toLowerCase(),
    changed: () => kept !== null || !same(current(), f.default),
    read: () => (kept !== null ? undefined : current()),
    apply: setValue,
    node: h("div", { class: "opt-row" },
      h("div", { class: "opt-info" },
        h("div", { class: "opt-title" }, h("label", { for: id, class: "field-label" }, f.label),
          h("code", { class: "opt-key muted" }, f.key), reset),
        docBlock(f.doc, f.rich)),
      h("div", { class: "opt-input" }, keptNote, inputs)),
  };

  if (initial && "kept" in initial) {
    control.set(concrete);
    keptNote.append(
      h("div", { class: "small" }, withNodes(t("The YAML sets {value}, which the form cannot show. It stays as it is."),
        { value: h("code", {}, initial.kept) })),
      h("button", { class: "btn sm", type: "button", onclick: () => { setValue(f.default); ctx.changed(); } },
        icon("edit", 13), t("Set it here")));
  } else if (initial && "random" in initial) {
    setValue("random");
  } else {
    setValue(initial && "value" in initial ? initial.value : f.default);
  }
  control.onChange(() => { sync(); ctx.changed(); });
  if (random) random.onchange = () => { sync(); ctx.changed(); };
  reset.onclick = () => { setValue(f.default); ctx.changed(); };
  sync();
  return row;
}

// Sets compare without their order, counters without their zeros
function same(a, b) {
  const norm = (v) => (Array.isArray(v) ? [...v].map(String).sort()
    : v && typeof v === "object" ? Object.entries(v).filter(([, n]) => n).sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0)) : v);
  return JSON.stringify(norm(a)) === JSON.stringify(norm(b));
}

// The option's own description. `rich` ones are reStructuredText, where a single line break only wraps the text; in the
// others it is meant. **bold**, ``code`` and lists are shown as such in both.
function docBlock(text, rich) {
  if (!text) return null;
  const paragraphs = text.split(/\n\s*\n/).map((p) => p.replace(/^\n+|\s+$/g, "")).filter(Boolean);
  const first = h("div", { class: "opt-doc" }, paragraph(paragraphs[0], rich));
  if (paragraphs.length === 1) return first;
  const rest = h("div", { class: "opt-doc", hidden: true }, paragraphs.slice(1).map((p) => paragraph(p, rich)));
  const more = h("button", { class: "btn ghost sm opt-more", type: "button" }, t("More"));
  more.onclick = () => {
    rest.hidden = !rest.hidden;
    more.textContent = rest.hidden ? t("More") : t("Less");
  };
  return h("div", {}, first, rest, more);
}

const BULLET = /^\s*[-*•]\s+/;

function paragraph(text, rich) {
  const lines = text.split("\n");
  if (!BULLET.test(lines[0])) return h("p", {}, inline(lines.map((line) => line.trim()).join(rich ? " " : "\n")));
  const items = [];
  for (const line of lines) {
    if (BULLET.test(line) || !items.length) items.push(line.replace(BULLET, ""));
    else items[items.length - 1] += ` ${line.trim()}`;
  }
  return h("ul", {}, items.map((item) => h("li", {}, inline(item))));
}

function inline(text) {
  return text.split(/(\*\*[^*]+\*\*|``[^`]+``|`[^`]+`)/).filter(Boolean).map((part) => {
    if (part.startsWith("**") && part.endsWith("**")) return h("strong", {}, part.slice(2, -2));
    if (part.startsWith("`") && part.endsWith("`")) return h("code", {}, part.replace(/^`+|`+$/g, ""));
    return part;
  });
}

// ---------------------------------------------------------------------------------------------------------------------
// controls: {node, get(), set(value), disable(flag), onChange(fn)}
// ---------------------------------------------------------------------------------------------------------------------
function makeControl(f, id, ctx) {
  switch (f.type) {
    case "toggle": return toggleControl(id);
    case "choice": return choiceControl(f, id);
    case "text_choice": return textChoiceControl(f, id);
    case "range": return rangeControl(f, id);
    case "text": return textControl(id);
    case "counter": return f.keys?.length && f.keys.length <= 60 ? amountsControl(f, id) : pickerControl(f, id, ctx, true);
    default: return f.keys?.length && f.keys.length <= 60 && !f.names ? checksControl(f, id) : pickerControl(f, id, ctx, false);
  }
}

function listeners() {
  const fns = [];
  return { add: (fn) => fns.push(fn), fire: () => fns.forEach((fn) => fn()) };
}

function toggleControl(id) {
  const on = listeners();
  const input = h("input", { type: "checkbox", class: "switch", id, onchange: () => { sync(); on.fire(); } });
  const text = h("span", {});
  const sync = () => { text.textContent = input.checked ? t("Yes") : t("No"); };
  return {
    node: h("label", { class: "switch-wrap" }, input, text),
    get: () => input.checked,
    set: (value) => { input.checked = value === true; sync(); },
    disable: (flag) => { input.disabled = flag; },
    onChange: on.add,
  };
}

function choiceControl(f, id) {
  const on = listeners();
  const input = select(f.choices.map(([name, label]) => [name, label]), f.default);
  input.id = id;
  input.onchange = on.fire;
  return {
    node: input,
    get: () => input.value,
    set: (value) => { if (f.choices.some(([name]) => name === value)) input.value = value; },
    disable: (flag) => { input.disabled = flag; },
    onChange: on.add,
  };
}

// The choices, or a text of one's own (a boss plando, say)
function textChoiceControl(f, id) {
  const on = listeners();
  const CUSTOM = "__other__";
  const input = select([...f.choices.map(([name, label]) => [name, label]), [CUSTOM, t("Other…")]], f.default);
  input.id = id;
  const custom = h("input", { class: "input", placeholder: t("Your own value"), "aria-label": t("Your own value"), hidden: true });
  input.onchange = () => {
    custom.hidden = input.value !== CUSTOM;
    if (!custom.hidden) custom.focus();
    on.fire();
  };
  custom.oninput = on.fire;
  return {
    node: h("div", { class: "stack tight" }, input, custom),
    get: () => (input.value === CUSTOM ? custom.value : input.value),
    set: (value) => {
      const known = f.choices.some(([name]) => name === value);
      input.value = known ? value : CUSTOM;
      custom.value = known ? "" : String(value ?? "");
      custom.hidden = known;
    },
    disable: (flag) => { input.disabled = flag; custom.disabled = flag; },
    onChange: on.add,
  };
}

function words(name) {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// A number, with a slider when the range is small enough to slide through, and the range's named values (if any)
function rangeControl(f, id) {
  const on = listeners();
  const named = new Map(f.named.map(([name, value]) => [value, name]));
  const number = h("input", { class: "input opt-number", type: "number", id, min: f.min, max: f.max, step: 1 });
  const slider = f.max - f.min > 0 && f.max - f.min <= 1000
    ? h("input", { type: "range", class: "opt-slider", min: f.min, max: f.max, step: 1, "aria-label": t("Value") }) : null;
  const CUSTOM = "__other__";
  const namedSelect = f.named.length
    ? select([...f.named.map(([name, value]) => [String(value), `${words(name)} (${value})`]), [CUSTOM, t("Other value")]], CUSTOM)
    : null;
  const show = (value) => {
    number.value = String(value);
    if (slider) slider.value = String(Math.min(f.max, Math.max(f.min, value)));
    if (namedSelect) namedSelect.value = named.has(value) ? String(value) : CUSTOM;
  };
  const valid = (value) => Number.isInteger(value) && ((value >= f.min && value <= f.max) || named.has(value));
  number.oninput = () => {
    const value = Number(number.value);
    if (number.value !== "" && Number.isInteger(value)) {
      if (slider) slider.value = String(Math.min(f.max, Math.max(f.min, value)));
      if (namedSelect) namedSelect.value = named.has(value) ? String(value) : CUSTOM;
    }
    on.fire();
  };
  number.onchange = () => {  // when the field is left: back into the range
    const value = Math.round(Number(number.value));
    show(valid(value) ? value : Number.isFinite(value) ? Math.min(f.max, Math.max(f.min, value)) : f.min);
    on.fire();
  };
  if (slider) slider.oninput = () => { show(Number(slider.value)); on.fire(); };
  if (namedSelect) namedSelect.onchange = () => { if (namedSelect.value !== CUSTOM) show(Number(namedSelect.value)); else number.focus(); on.fire(); };
  return {
    node: h("div", { class: "stack tight" }, namedSelect, h("div", { class: "row opt-range" }, slider, number),
      h("span", { class: "hint" }, t("{min} to {max}", { min: f.min, max: f.max }))),
    get: () => {
      const value = Number(number.value);
      return number.value !== "" && Number.isInteger(value) ? value : number.value;
    },
    set: (value) => show(typeof value === "number" ? value : f.min),
    disable: (flag) => { for (const el of [number, slider, namedSelect]) if (el) el.disabled = flag; },
    onChange: on.add,
  };
}

function textControl(id) {
  const on = listeners();
  const input = h("input", { class: "input", id, oninput: on.fire });
  return {
    node: input,
    get: () => input.value,
    set: (value) => { input.value = value == null ? "" : String(value); },
    disable: (flag) => { input.disabled = flag; },
    onChange: on.add,
  };
}

// A few fixed values to tick
function checksControl(f, id) {
  const on = listeners();
  const boxes = f.keys.map((key, i) => {
    const input = h("input", { type: "checkbox", id: i === 0 ? id : undefined, onchange: on.fire });
    return { key: String(key), input, node: h("label", { class: "check" }, input, h("span", {}, String(key))) };
  });
  return {
    node: h("div", { class: "opt-checks" }, boxes.map((box) => box.node)),
    get: () => boxes.filter((box) => box.input.checked).map((box) => box.key),
    set: (value) => { const chosen = new Set(Array.isArray(value) ? value.map(String) : []); for (const box of boxes) box.input.checked = chosen.has(box.key); },
    disable: () => {},
    onChange: on.add,
  };
}

// A number for each of a few fixed keys (weights of traps, say)
function amountsControl(f, id) {
  const on = listeners();
  const inputs = f.keys.map((key, i) => [String(key), h("input", { class: "input opt-number", type: "number", step: 1, min: f.min ?? undefined,
    max: f.max ?? undefined, id: i === 0 ? id : undefined, "aria-label": String(key), oninput: on.fire })]);
  return {
    node: h("div", { class: "opt-amounts" }, inputs.map(([key, input]) => h("label", { class: "opt-amount" }, h("span", {}, key), input))),
    get: () => Object.fromEntries(inputs.map(([key, input]) => [key, amount(input.value)])),
    set: (value) => { for (const [key, input] of inputs) input.value = String(value?.[key] ?? 0); },
    disable: () => {},
    onChange: on.add,
  };
}

function amount(text) {
  const value = Number(text);
  return text !== "" && Number.isInteger(value) ? value : text;
}

// Names picked from a long list (items, locations, or many fixed values): typed with suggestions, shown as chips. With
// `amounts`, each name has a number (start inventory).
function pickerControl(f, id, ctx, amounts) {
  const on = listeners();
  const { schema, lists } = ctx;
  const pool = f.keys?.length ? f.keys.map(String) : f.names === "items" ? schema.items : f.names === "locations" ? schema.locations : null;
  const groups = !f.keys?.length && f.groups ? (f.names === "items" ? schema.item_groups : schema.location_groups) : [];
  const known = pool ? new Map([...groups, ...pool].map((name) => [name.toLowerCase(), name])) : null;
  let listId = null;
  if (pool) {
    listId = f.keys?.length ? `names-${f.key}` : `names-${f.names}${groups.length ? "-groups" : ""}`;
    if (!lists.has(listId)) {
      lists.set(listId, h("datalist", { id: listId },
        groups.map((name) => h("option", { value: name, label: t("group") })), pool.map((name) => h("option", { value: name }))));
    }
  }
  let entries = new Map();  // name -> amount (1 for plain sets)
  const chips = h("div", { class: "opt-chips" });
  const input = h("input", { class: "input", id, list: listId, autocomplete: "off",
    placeholder: pool ? (f.names === "locations" ? t("Type a location…") : f.names === "items" ? t("Type an item…") : t("Type a value…"))
      : t("Type a value…") });
  const count = amounts ? h("input", { class: "input opt-number", type: "number", value: 1, step: 1, min: f.min ?? undefined,
    max: f.max ?? undefined, "aria-label": t("Amount") }) : null;
  const error = h("div", { class: "form-error small", hidden: true });
  const add = () => {
    const typed = input.value.trim();
    if (!typed) return;
    const name = known ? known.get(typed.toLowerCase()) : typed;
    if (!name) {
      error.hidden = false;
      error.textContent = t("“{name}” is not one of this game's names.", { name: typed });
      return;
    }
    error.hidden = true;
    entries.set(name, amounts ? amount(count.value) : 1);
    input.value = "";
    if (count) count.value = "1";
    draw();
    on.fire();
    input.focus();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  input.addEventListener("input", () => { error.hidden = true; });
  // Picking a suggestion with the mouse fills the field; adding it right away saves a click
  input.addEventListener("change", () => { if (known?.has(input.value.trim().toLowerCase()) && !amounts) add(); });

  function draw() {
    clear(chips).append([...entries].map(([name, n]) => {
      const remove = h("button", { class: "chip-remove", type: "button", "aria-label": t("Remove {name}", { name }), title: t("Remove"),
        onclick: () => { entries.delete(name); draw(); on.fire(); } }, icon("x", 12));
      if (!amounts) return h("span", { class: `chip${groups.includes(name) ? " group" : ""}` }, name, remove);
      const number = h("input", { class: "input opt-number sm", type: "number", step: 1, value: n, "aria-label": t("Amount of {name}", { name }),
        oninput: () => { entries.set(name, amount(number.value)); on.fire(); } });
      return h("span", { class: "chip amount" }, number, h("span", {}, name), remove);
    }));
    if (!entries.size) chips.append(h("span", { class: "muted small" }, t("None")));
  }
  draw();
  return {
    node: h("div", { class: "stack tight" }, chips,
      h("div", { class: "row opt-add" }, h("div", { class: "grow" }, input), count,
        h("button", { class: "btn sm", type: "button", onclick: add }, icon("plus", 13), t("Add"))), error),
    get: () => (amounts ? Object.fromEntries(entries) : [...entries.keys()]),
    set: (value) => {
      entries = new Map(amounts ? Object.entries(value || {}) : (Array.isArray(value) ? value : []).map((name) => [String(name), 1]));
      draw();
    },
    disable: () => {},
    onChange: on.add,
  };
}
