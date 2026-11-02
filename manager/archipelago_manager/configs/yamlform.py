"""The options form of the config editor: reads a player yaml into the values the form shows, and writes the form's
values back into the yaml.

The form edits one player of one game, with the options Archipelago describes for that game (ap_helper.py writes those
descriptions next to the templates). Everything else in the file stays as it is: values the form cannot show (weights
over several choices, random ranges), options it has no field for (plando, item links), unknown keys and top-level
settings such as `requires` or `triggers`. Comments are lost when the form rewrites a file.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import yaml

from .yamlinfo import load_documents


class NoFormError(Exception):
    """The file is one the form cannot edit (several players, a rolled game); the message says why."""


class FormError(ValueError):
    """A value the form sent that the option does not take."""


# ----------------------------------------------------------------------------------------------------------------------
# what the form knows about a game
# ----------------------------------------------------------------------------------------------------------------------
_SCHEMAS: dict[Path, tuple[float, dict]] = {}
_SCHEMAS_LOCK = threading.Lock()


def load_schema(path: Path) -> dict:
    """A game's options as ap_helper.py described them. The last few are kept in memory: a big game's is half a MB."""
    mtime = path.stat().st_mtime
    with _SCHEMAS_LOCK:
        cached = _SCHEMAS.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    schema = json.loads(path.read_text(encoding="utf-8"))
    with _SCHEMAS_LOCK:
        while len(_SCHEMAS) >= 8:
            _SCHEMAS.pop(next(iter(_SCHEMAS)))
        _SCHEMAS[path] = (mtime, schema)
    return schema


def fields(schema: dict) -> dict[str, dict]:
    return {field["key"]: field for group in schema["groups"] for field in group["options"]}


def allowed_names(field: dict, schema: dict) -> set[str] | None:
    """The values a set, list or counter may hold; None if any text will do."""
    if field.get("keys"):
        return {str(key) for key in field["keys"]}
    if field.get("names") == "items":
        return set(schema["items"]) | (set(schema["item_groups"]) if field.get("groups") else set())
    if field.get("names") == "locations":
        return set(schema["locations"]) | (set(schema["location_groups"]) if field.get("groups") else set())
    return None


# ----------------------------------------------------------------------------------------------------------------------
# yaml -> form
# ----------------------------------------------------------------------------------------------------------------------
_KEEP = object()  # a value the form cannot show; it stays in the file as it is
_TOGGLE_OFF = {"off", "0", "false", "none", "null", "no", "disabled"}  # what Archipelago's Toggle accepts
_TOGGLE_ON = {"on", "1", "true", "yes", "enabled"}


def single_player(text: str) -> tuple[dict, str]:
    """The one player and the one game of a file the form can edit. Raises NoFormError if the form cannot edit it, and
    yamlinfo.YamlError if it is no yaml at all."""
    documents = [doc for doc in load_documents(text) if doc is not None]
    if len(documents) != 1:
        raise NoFormError("This file holds several players. The form edits one player at a time; "
                          "use the YAML editor for this file.")
    doc = documents[0]
    if not isinstance(doc, dict):
        raise NoFormError("This file is not a list of options. Use the YAML editor for it.")
    game = _rolled(doc.get("game"))
    if game is _KEEP:
        raise NoFormError("The game is picked at random from several. The form edits a file for one game; "
                          "use the YAML editor for this file.")
    if not isinstance(game, str) or not game.strip():
        raise NoFormError("The file does not say which game it is for. Set game: in the YAML editor.")
    if isinstance(doc.get("name"), (dict, list)):
        raise NoFormError("The player name is picked at random from several. Use the YAML editor for this file.")
    return doc, game.strip()


def _rolled(raw: Any) -> Any:
    """What a weighted value always rolls to: Archipelago picks a key of a mapping by weight, or an entry of a list.
    _KEEP when it can roll more than one value, or none."""
    if isinstance(raw, dict):
        try:
            picks = [key for key, weight in raw.items() if int(weight) != 0]
        except (TypeError, ValueError):
            return _KEEP
        return picks[0] if len(picks) == 1 else _KEEP
    if isinstance(raw, list):
        return raw[0] if len(raw) == 1 else _KEEP
    return raw


def _as_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _read_value(field: dict, raw: Any, schema: dict) -> Any:
    """The form's value for what the yaml has: a plain value, "random", or _KEEP."""
    kind = field["type"]
    if kind in ("set", "list"):
        if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
            return _KEEP
        allowed = allowed_names(field, schema)
        if allowed is not None and not set(raw) <= allowed:
            return _KEEP
        return list(dict.fromkeys(raw)) if kind == "set" else list(raw)
    if kind == "counter":
        if not isinstance(raw, dict):
            return _KEEP
        allowed = allowed_names(field, schema)
        for key, amount in raw.items():
            if not isinstance(key, str) or (allowed is not None and key not in allowed):
                return _KEEP
            if type(amount) is not int or not _within(amount, field.get("min"), field.get("max")):
                return _KEEP
        return dict(raw)

    value = _rolled(raw)
    if value is _KEEP:
        return _KEEP
    if kind == "toggle":
        if isinstance(value, (bool, int)):
            return bool(value)
        if value == "random":
            return "random"
        if isinstance(value, str) and value.lower() in _TOGGLE_OFF | _TOGGLE_ON:
            return value.lower() in _TOGGLE_ON
        return _KEEP
    if kind in ("choice", "text_choice"):
        if type(value) is int:
            for name, _label, number in field["choices"]:
                if number == value:
                    return name
        text = _as_text(value)
        if text is None:
            return _KEEP
        if text.lower() == "random":
            return "random"
        names = {choice[0] for choice in field["choices"]}
        if text.lower() in names:
            return text.lower()
        if text.lower() in field.get("aliases", {}):
            return field["aliases"][text.lower()]
        return text if kind == "text_choice" else _KEEP
    if kind == "range":
        named = {name: number for name, number in field["named"]}
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "random":
                return "random"
            if text in named:
                return named[text]
            if text in ("low", "high"):
                return field["min"] if text == "low" else field["max"]
            if text == "default":
                return field["default"]
            try:
                value = int(text)
            except ValueError:
                return _KEEP  # random-low, random-range-5-10 and the like
        if type(value) is int and (field["min"] <= value <= field["max"] or value in named.values()):
            return value
        return _KEEP
    if kind == "text":
        return value if isinstance(value, str) else _as_text(value) if isinstance(value, (int, float)) else _KEEP
    return _KEEP


def _within(value: int, low: int | None, high: int | None) -> bool:
    return (low is None or value >= low) and (high is None or value <= high)


def _summary(raw: Any) -> str:
    """A short look at a value the form keeps as it is, in yaml's one-line style."""
    text = yaml.safe_dump(raw, default_flow_style=True, allow_unicode=True, sort_keys=False, width=10 ** 6).strip()
    text = text.removesuffix("...").strip()
    return text if len(text) <= 160 else text[:159] + "…"


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (list, dict, str)) and not value)


def form_state(doc: dict, game: str, schema: dict) -> dict:
    """What the form shows for a file: name, description, and for each option the yaml sets {"value": ...},
    {"random": true} or {"kept": "<summary>"}. Options the yaml leaves out are missing; they have their default."""
    section = doc.get(game) if isinstance(doc.get(game), dict) else {}
    known = fields(schema)
    options = {}
    for key, raw in section.items():
        field = known.get(key)
        if field is None:
            continue
        value = _read_value(field, raw, schema)
        if value == "random" and field["type"] != "text":  # for text, "random" is just a word
            options[key] = {"random": True} if field["random"] else {"kept": _summary(raw)}
        elif value is _KEEP:
            options[key] = {"kept": _summary(raw)}
        else:
            options[key] = {"value": value}
    # Mentioned in the form so nobody wonders where they went: settings it has no field for
    others = [str(key) for key, value in section.items() if key not in known and not _empty(value)]
    others += [str(key) for key, value in doc.items()
               if key not in ("name", "description", "game", "requires", game) and not _empty(value)]
    name, description = doc.get("name"), doc.get("description")
    return {
        "game": game,
        "name": "" if name is None else str(name),
        "description": "" if description is None else str(description),
        "options": options,
        "others": others,
    }


# ----------------------------------------------------------------------------------------------------------------------
# form -> yaml
# ----------------------------------------------------------------------------------------------------------------------
def _shown(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:59] + "…"


def checked_value(field: dict, value: Any, schema: dict) -> Any:
    """The value as it goes into the yaml. Raises FormError if the option does not take it."""
    label, kind = field["label"], field["type"]
    if value == "random" and field["random"]:
        return "random"
    if kind == "toggle" and isinstance(value, bool):
        return value
    if kind == "choice" and isinstance(value, str) and value in {choice[0] for choice in field["choices"]}:
        return value
    if kind in ("text_choice", "text") and isinstance(value, str) and len(value) <= 10_000:
        return value
    if kind == "range":
        named = {number for _name, number in field["named"]}
        if type(value) is int and (field["min"] <= value <= field["max"] or value in named):
            return value
        raise FormError(f"{label}: {_shown(value)} is not a whole number from {field['min']} to {field['max']}")
    if kind in ("set", "list") and isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        allowed = allowed_names(field, schema)
        unknown = [entry for entry in value if allowed is not None and entry not in allowed]
        if unknown:
            raise FormError(f"{label}: unknown names: {_shown(unknown)}")
        return list(dict.fromkeys(value)) if kind == "set" else value
    if kind == "counter" and isinstance(value, dict):
        allowed = allowed_names(field, schema)
        unknown = [key for key in value if allowed is not None and key not in allowed]
        if unknown:
            raise FormError(f"{label}: unknown names: {_shown(unknown)}")
        result = {}
        for key, amount in value.items():
            if type(amount) is not int:
                raise FormError(f"{label}: {key} needs a whole number")
            if not _within(amount, field.get("min"), field.get("max")):
                raise FormError(f"{label}: {key} must be from {field.get('min')} to {field.get('max')}")
            # A counter of items or locations leaves out what it has none of, as Archipelago does; a counter with
            # fixed keys (weights of traps, say) means its zeros.
            if amount or field.get("keys"):
                result[key] = amount
        return result
    raise FormError(f"{label}: {_shown(value)} is not one of its values")


def write(text: str | None, game: str, schema: dict, *, name: str, description: str, values: dict[str, Any]) -> str:
    """The yaml for the form's values. `text` is the file being edited (None for a new one): whatever the form does not
    set is taken from it unchanged. `values` holds the options the form sets; options it keeps are left out."""
    doc: dict = {}
    if text and text.strip():
        doc, file_game = single_player(text)
        if file_game != game:
            raise FormError(f"The file is for {file_game}, not {game}")
    name = name.strip()
    if not name:
        raise FormError("Enter a player name")
    known = fields(schema)
    section = dict(doc[game]) if isinstance(doc.get(game), dict) else {}
    for key, value in values.items():
        if key not in known:
            raise FormError(f"{game} has no option {key}")
        section[key] = checked_value(known[key], value, schema)

    result: dict[Any, Any] = {"name": name}
    if description.strip():
        result["description"] = description.strip()
    result["game"] = game
    for key, value in doc.items():
        if key not in ("name", "description", "game", game):
            result[key] = value
    result[game] = section
    return yaml.safe_dump(result, sort_keys=False, allow_unicode=True, width=1000)
