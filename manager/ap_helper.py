#!/usr/bin/env python3
"""Bridge between the manager and an Archipelago installation.

This script runs inside the *Archipelago* Python environment (not the manager's), because everything it does
needs AP's own modules: listing installed worlds, decoding multidata/save files with AP's restricted unpickler and
rendering yaml templates.

Usage: ap_helper.py --ap-dir DIR COMMAND [ARGS]

Every command prints exactly one result line prefixed with SENTINEL so the caller can ignore any noise AP writes to
stdout while importing (logging, warnings).
"""
import argparse
import json
import os
import sys
import traceback
import zipfile
import zlib

SENTINEL = "@@APJSON@@"
MAX_FORMAT_VERSION = 3  # MultiServer.Context.decompress refuses anything newer


def emit(obj) -> None:
    sys.stdout.write("\n" + SENTINEL + json.dumps(obj) + "\n")
    sys.stdout.flush()


def fail(message: str, code: int = 1) -> None:
    emit({"ok": False, "error": message})
    sys.exit(code)


def setup(ap_dir: str) -> None:
    ap_dir = os.path.abspath(ap_dir)
    if not os.path.isfile(os.path.join(ap_dir, "Utils.py")):
        fail(f"{ap_dir} is not an Archipelago source directory")
    os.chdir(ap_dir)
    sys.path.insert(0, ap_dir)
    os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "true")


def version_str(version) -> str | None:
    if version is None:
        return None
    if hasattr(version, "as_simple_string"):
        return version.as_simple_string()
    if isinstance(version, (tuple, list)):
        return ".".join(str(part) for part in version)
    return str(version)


# --------------------------------------------------------------------------------------------------------------------
# where: cheap, only imports Utils. Used to find out where custom worlds have to be placed *before* worlds is imported.
# --------------------------------------------------------------------------------------------------------------------
def cmd_where(_args) -> dict:
    import Utils
    user = Utils.user_path()
    local = Utils.local_path()
    # same formula as worlds/__init__.py
    worlds_folder = Utils.user_path("worlds") if user != local else Utils.user_path("custom_worlds")
    os.makedirs(worlds_folder, exist_ok=True)
    return {
        "version": Utils.__version__,
        "version_tuple": list(Utils.version_tuple),
        "user_path": user,
        "local_path": local,
        "worlds_folder": worlds_folder,
        "host_yaml": Utils.user_path("host.yaml"),
    }


# --------------------------------------------------------------------------------------------------------------------
# games: everything that was successfully (or unsuccessfully) loaded
# --------------------------------------------------------------------------------------------------------------------
def expected_path(error: FileNotFoundError) -> str | None:
    """Where a world looked for a file, from the ways worlds report a missing base ROM."""
    import Utils
    text = error.filename or (str(error.args[0]) if error.args else "")
    text = text.split(" does not exist, but ")[0]  # settings' message for a required file that is missing
    if ": " in text and not os.path.isabs(text):  # "Could not find base ROM for <game>: <path>"
        text = text.rsplit(": ", 1)[-1]
    return os.path.abspath(Utils.user_path(text)) if text else None


def generation_file_problem(world, probe) -> dict | None:
    """Runs a world's own check before generating (stage_assert_generate), as Generate.py does, but without players.

    Worlds that need a base ROM to generate raise FileNotFoundError there when it is missing. Anything else is left to
    the real generation, which runs the check again with the players' options.
    """
    from worlds.AutoWorld import World
    if getattr(world.stage_assert_generate, "__func__", None) is World.stage_assert_generate.__func__:
        return None  # the default check does nothing
    try:
        world.stage_assert_generate(probe)
    except FileNotFoundError as e:
        return {"path": expected_path(e), "message": str(e)}
    except Exception:  # noqa: BLE001 - checks that need players, or a broken world: not ours to judge here
        return None
    return None


def rom_files(world, probe) -> list[dict]:
    """The base ROMs a world asks for in host.yaml: its file settings with known checksums. For each, where it goes,
    whether it is there and the right one, and whether generating needs it.

    Whether generating needs it is found by running the world's own check with the file missing. That happens in
    memory only and the setting is put back right after: host.yaml is not changed. Run this before the world's real
    check, which may load and keep the ROM.
    """
    import settings
    from worlds.AutoWorld import World
    key = getattr(world, "settings_key", None)
    if not key:
        return []
    try:
        group = getattr(settings.get_settings(), key)
    except Exception:  # noqa: BLE001 - a world without settings of its own
        return []
    own_check = getattr(world.stage_assert_generate, "__func__", None) is not World.stage_assert_generate.__func__
    found = []
    for field in getattr(type(group), "__annotations__", {}):
        try:
            value = object.__getattribute__(group, field)  # not through Group's, which would ask for missing files
        except AttributeError:
            continue
        if not isinstance(value, settings.FilePath) or value.is_exe or not type(value).md5s:
            continue
        path = os.path.abspath(value.resolve())
        entry = {"setting": f"{key}.{field}", "path": path, "description": value.description or "",
                 "valid": None, "needed": False}
        if os.path.exists(path):
            try:
                type(value).validate(path)
                entry["valid"] = True
            except Exception:  # noqa: BLE001 - the file is there, but not the right one
                entry["valid"] = False
        if own_check:
            object.__setattr__(group, field, type(value)(path + ".missing-for-a-check"))
            try:
                world.stage_assert_generate(probe)
            except FileNotFoundError:
                entry["needed"] = True
            except Exception:  # noqa: BLE001 - checks that need players: not ours to judge
                pass
            finally:
                object.__setattr__(group, field, value)
        found.append(entry)
    return found


def cmd_games(args) -> dict:
    where = cmd_where(args)
    import settings
    import worlds
    from BaseClasses import MultiWorld
    from worlds.AutoWorld import AutoWorldRegister
    settings.no_gui = True  # a missing ROM must raise instead of opening a file dialog
    probe = MultiWorld(0)

    games = []
    for name, world in sorted(AutoWorldRegister.world_types.items(), key=lambda kv: kv[0].lower()):
        module = sys.modules.get(world.__module__)
        file = getattr(module, "__file__", "") or ""
        apworld_file = None
        if ".apworld" in file:
            apworld_file = os.path.basename(file.split(".apworld")[0]) + ".apworld"
        in_user_folder = bool(worlds.user_folder) and os.path.abspath(file).startswith(os.path.abspath(worlds.user_folder))
        games.append({
            "name": name,
            "source": "custom" if (apworld_file or in_user_folder) else "builtin",
            "apworld_file": apworld_file,
            "module": world.__module__,
            "version": version_str(getattr(world, "world_version", None)) if getattr(world, "world_version", None) else None,
            "hidden": bool(getattr(world, "hidden", False)),
            "items": len(getattr(world, "item_name_to_id", {})),
            "locations": len(getattr(world, "location_name_to_id", {})),
            "roms": rom_files(world, probe),  # first: the world's own check below may load and keep its ROM
            "needs_file": generation_file_problem(world, probe),
        })
    return {
        **where,
        "games": games,
        "failed": dict(getattr(worlds, "failed_world_loads", {})),
    }


# --------------------------------------------------------------------------------------------------------------------
# multidata
# --------------------------------------------------------------------------------------------------------------------
def read_multidata_bytes(path: str) -> bytes:
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".archipelago"):
                    return zf.read(name)
        raise ValueError("No .archipelago file found in archive.")
    with open(path, "rb") as f:
        return f.read()


def decode_multidata(path: str):
    import Utils
    data = read_multidata_bytes(path)
    if not data:
        raise ValueError("Empty multidata file.")
    if data[0] > MAX_FORMAT_VERSION:
        raise ValueError("Incompatible multidata (format is newer than this Archipelago version supports).")
    return Utils.restricted_loads(zlib.decompress(data[1:]))


def cmd_multidata(args) -> dict:
    where = cmd_where(args)
    md = decode_multidata(args.path)
    connect_names = {name: tuple(v) for name, v in md.get("connect_names", {}).items()}
    slot_for_name = {slot: name for name, (_team, slot) in connect_names.items()}
    locations = md.get("locations", {})
    slots = []
    for slot, info in sorted(md["slot_info"].items()):
        slot_type = int(getattr(info, "type", 1))
        slots.append({
            "slot": int(slot),
            "name": info.name,
            "game": info.game,
            # 0 = spectator, 1 = player, 2 = group
            "type": slot_type,
            "group_members": [int(m) for m in getattr(info, "group_members", ()) or ()],
            "locations": len(locations.get(slot, {})),
            "connect_name": slot_for_name.get(slot, info.name),
        })
    minimum = md.get("minimum_versions", {})
    return {
        "server_version": where["version"],
        "seed_name": md.get("seed_name"),
        "generator_version": list(md.get("version", ())),
        "minimum_server_version": list(minimum.get("server", ())),
        "race_mode": bool(md.get("race_mode", 0)),
        "games": sorted(set(md.get("datapackage", {}).keys()) - {"Archipelago"}),
        "server_options": {k: v for k, v in (md.get("server_options") or {}).items()
                           if isinstance(v, (str, int, float, bool, type(None)))},
        "slots": slots,
    }


# --------------------------------------------------------------------------------------------------------------------
# progress, from the server's .apsave
# --------------------------------------------------------------------------------------------------------------------
def cmd_progress(args) -> dict:
    cmd_where(args)
    save = load_save(args.save)

    slots: dict[int, dict] = {}

    def entry(slot: int) -> dict:
        return slots.setdefault(int(slot), {"checked": 0, "status": 0, "last_activity": None, "hints_used": 0})

    for (team, slot), checks in save.get("location_checks", {}).items():
        if team == 0:
            entry(slot)["checked"] = len(checks)
    for (team, slot), status in save.get("client_game_state", {}).items():
        if team == 0:
            entry(slot)["status"] = int(status)
    for (team, slot), ts in save.get("client_activity_timers", ()):
        if team == 0:
            entry(slot)["last_activity"] = ts
    for (team, slot), used in save.get("hints_used", {}).items():
        if team == 0:
            entry(slot)["hints_used"] = int(used)
    # Every hint is stored for both the finding and the receiving player; list each once.
    hints = set()
    for (team, _slot), slot_hints in save.get("hints", {}).items():
        if team == 0:
            for h in slot_hints:
                hints.add((int(h.receiving_player), int(h.finding_player), int(h.location), int(h.item),
                           str(h.entrance or ""), int(h.item_flags)))
    checks = {int(slot): sorted(int(loc) for loc in locs)
              for (team, slot), locs in save.get("location_checks", {}).items() if team == 0}
    return {"slots": slots, "hints": sorted(hints), "checks": checks}


class Namer:
    """Player, item and location names of one multiworld, from its own data package (custom games included)."""

    def __init__(self, md) -> None:
        self.md = md
        self.package = md.get("datapackage", {})
        self.reverse: dict[tuple[str, str], dict[int, str]] = {}

    def _names(self, kind: str, game: str) -> dict[int, str]:
        if (kind, game) not in self.reverse:
            mapping = self.package.get(game, {}).get(f"{kind}_name_to_id", {})
            self.reverse[kind, game] = {value: name for name, value in mapping.items()}
        return self.reverse[kind, game]

    def lookup(self, kind: str, slot: int, ident: int) -> str:
        """An item id in the game of the slot receiving it, or a location id in the game of the slot it is in."""
        info = self.md["slot_info"].get(slot)
        # ids below zero (the server, cheats, starting inventory) belong to the "Archipelago" pseudo game
        for game in ((info.game,) if info else ()) + ("Archipelago",):
            if ident in self._names(kind, game):
                return self._names(kind, game)[ident]
        return f"{kind.capitalize()} {ident}"

    def player(self, slot: int) -> str:
        info = self.md["slot_info"].get(slot)
        return info.name if info else "Archipelago"


def load_save(path: str) -> dict:
    import Utils
    with open(path, "rb") as f:
        return Utils.restricted_loads(zlib.decompress(f.read()))


def cmd_names(args) -> dict:
    """Names for ids. The request lists [slot, id] pairs: items in the game of the slot receiving them, locations in
    the game of the slot they are in."""
    cmd_where(args)
    md = decode_multidata(args.path)
    request = json.loads(args.request)
    names = Namer(md)
    return {
        "players": {int(slot): info.name for slot, info in md["slot_info"].items()},
        "items": [[slot, ident, names.lookup("item", slot, ident)] for slot, ident in request.get("items", [])],
        "locations": [[slot, ident, names.lookup("location", slot, ident)]
                      for slot, ident in request.get("locations", [])],
    }


def cmd_tracker(args) -> dict:
    """One slot's view of a game, with names: the items it received (who sent them from where), its locations and
    whether they are checked, and the hints that concern it. Works before the first save, too."""
    cmd_where(args)
    md = decode_multidata(args.path)
    save = load_save(args.save) if os.path.exists(args.save) else {}
    names = Namer(md)
    slot = args.slot
    info = md["slot_info"].get(slot)
    if info is None:
        raise ValueError(f"There is no slot {slot} in this multiworld")

    items = [{"item": names.lookup("item", slot, item), "from": None, "location": None, "flags": 0, "start": True}
             for item in md.get("precollected_items", {}).get(slot, [])]
    for received in save.get("received_items", {}).get((0, slot, True), []):
        items.append({"item": names.lookup("item", slot, received.item), "from": names.player(received.player),
                      "location": names.lookup("location", received.player, received.location),
                      "flags": int(received.flags), "start": False})

    checked = save.get("location_checks", {}).get((0, slot), set())
    locations = [{"location": names.lookup("location", slot, location), "checked": location in checked}
                 for location in sorted(md.get("locations", {}).get(slot, {}))]

    hints = []
    for h in save.get("hints", {}).get((0, slot), set()):
        hints.append({"item": names.lookup("item", h.receiving_player, h.item),
                      "receiver": names.player(h.receiving_player), "for_me": h.receiving_player == slot,
                      "location": names.lookup("location", h.finding_player, h.location),
                      "finder": names.player(h.finding_player), "in_my_world": h.finding_player == slot,
                      "entrance": str(h.entrance or ""), "found": bool(h.found), "flags": int(h.item_flags),
                      "status": int(getattr(h, "status", 0)),
                      # what the server knows the hint by, for its receiver to change its status (UpdateHint)
                      "finder_slot": int(h.finding_player), "location_id": int(h.location)})
    hints.sort(key=lambda h: (h["found"], h["item"].lower()))
    status = save.get("client_game_state", {}).get((0, slot), 0)
    # This slot's items still waiting in some world, its own included: what !collect would bring
    checks = save.get("location_checks", {})
    items_left = sum(1 for finder, placed in md.get("locations", {}).items() for location, entry in placed.items()
                     if entry[1] == slot and location not in checks.get((0, finder), set()))
    # The rules the server ran with when it last saved (the save also holds the passwords, which stay here)
    options = save.get("game_options") or {}
    rules = {key: options[key] for key in ("hint_cost", "location_check_points", "release_mode", "collect_mode")
             if key in options} or None
    return {"slot": slot, "name": info.name, "game": info.game, "goal": int(status) == 30,
            "items": items, "locations": locations, "hints": hints,
            "hints_used": int(save.get("hints_used", {}).get((0, slot), 0)), "items_left": items_left,
            "server_rules": rules}


def cmd_build_save(args) -> dict:
    """A save for a multiworld from how a game stands elsewhere (an archipelago.gg room, ggimport.py): the checked
    locations, the items each slot received, the hints, aliases, client states and times, all of team 0. The server
    takes it like its own save. What it cannot know stays empty: hint points spent, the data storage. Server options
    are left out, so the game's own settings apply."""
    cmd_where(args)
    import pickle
    import random
    from NetUtils import Hint, HintStatus, NetworkItem
    md = decode_multidata(args.path)
    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)
    locations = md.get("locations", {})
    players = {int(slot) for slot, info in md["slot_info"].items() if int(getattr(info, "type", 1)) == 1}
    slot_of = lambda key: int(key)  # noqa: E731 - JSON keys are text
    different = "The items in it lie elsewhere than in this multiworld, so it is another one"

    location_checks = {}
    for key, checked in state.get("location_checks", {}).items():
        slot = slot_of(key)
        unknown = set(checked) - set(locations.get(slot, {}))
        if slot not in players or unknown:
            raise ValueError(f"Slot {slot} has locations this multiworld does not have")
        location_checks[0, slot] = set(checked)
    received_items = {}
    for key, items in state.get("received_items", {}).items():
        slot = slot_of(key)
        if slot not in players:
            raise ValueError(f"There is no player slot {slot} in this multiworld")
        received = [NetworkItem(int(i[0]), int(i[1]), int(i[2]), int(i[3]) if len(i) > 3 else 0) for i in items]
        for item in received:
            placed = locations.get(item.player, {}).get(item.location)
            # (items from the server, cheats and starting inventories have no location of a slot's)
            if item.location > 0 and item.player in players and (not placed or placed[0] != item.item or placed[1] != slot):
                raise ValueError(different)
        # as MultiServer.send_items_to keeps them: everything, and what came from other worlds
        received_items[0, slot, True] = received
        received_items[0, slot, False] = [item for item in received if item.player != slot]
    hints = {}
    for key, slot_hints in state.get("hints", {}).items():
        slot = slot_of(key)
        found = set()
        for h in slot_hints:
            placed = locations.get(int(h[1]), {}).get(int(h[2]))
            if not placed or placed[0] != int(h[3]) or placed[1] != int(h[0]):
                raise ValueError(different)
            status = HintStatus(int(h[7])) if len(h) > 7 else HintStatus.HINT_UNSPECIFIED
            found.add(Hint(int(h[0]), int(h[1]), int(h[2]), int(h[3]), bool(h[4]), str(h[5] or "") if len(h) > 5 else "",
                           int(h[6]) if len(h) > 6 else 0, status))
        hints[0, slot] = found
    timers = lambda name: tuple(((0, slot_of(k)), float(v)) for k, v in state.get(name, {}).items() if v)  # noqa: E731
    save = {
        "version": 2,
        "connect_names": md["connect_names"],
        "received_items": received_items,
        "hints_used": {},
        "hints": hints,
        "location_checks": location_checks,
        "name_aliases": {(0, slot_of(k)): str(v) for k, v in state.get("aliases", {}).items() if v},
        "client_game_state": {(0, slot_of(k)): int(v) for k, v in state.get("client_game_state", {}).items()},
        "client_activity_timers": timers("client_activity_timers"),
        "client_connection_timers": timers("client_connection_timers"),
        "random_state": random.Random().getstate(),
        "group_collected": {},
        "stored_data": {},
    }
    with open(args.out, "wb") as f:
        f.write(zlib.compress(pickle.dumps(save)))
    return {"checked": sum(len(v) for v in location_checks.values()),
            "received": sum(len(v) for (_team, _slot, everything), v in received_items.items() if everything),
            "hints": len({h for v in hints.values() for h in v})}


def cmd_roll(args) -> dict:
    """Rolls a player file the way the generator does (Generate.roll_settings): weights, random ranges, triggers and a
    game picked at random become one value each. The result is a file without chance in it: for every player, the game
    it got and each option the weights name or that comes out other than its default."""
    cmd_where(args)
    import yaml
    import Generate
    import worlds  # noqa: F401  (registers all worlds)
    from BaseClasses import PlandoOptions
    from worlds.AutoWorld import AutoWorldRegister
    every_plando = PlandoOptions.from_option_string("bosses, items, texts, connections")
    documents, rolled = [], []
    for weights in Generate.read_weights_yamls(args.path):
        if weights is None:
            continue
        settings = Generate.roll_settings(weights, every_plando)
        world = AutoWorldRegister.world_types[settings.game]
        raw = weights.get(settings.game) or {}
        options = {}
        for key, option in world.options_dataclass.type_hints.items():
            value = getattr(settings, key, None)
            if value is None:
                continue
            given = raw.get(key) if isinstance(raw, dict) else None
            if given is not None and not option.supports_weighting:
                options[key] = _plain(given)  # taken as written: plando, item and location lists
                continue
            try:
                shown = form_value(type(value), value.value)
                default = form_value(option, option.default)
            except Exception:  # noqa: BLE001 - an option the form cannot show is kept as the file has it
                if given is not None:
                    options[key] = _plain(given)
                continue
            if given is not None or shown != default:
                options[key] = shown
        document = {"name": settings.name, "game": settings.game, settings.game: options}
        if isinstance(weights.get("description"), str):
            document = {"name": settings.name, "description": weights["description"], "game": settings.game,
                        settings.game: options}
        if isinstance(weights.get("requires"), dict):
            document["requires"] = _plain(weights["requires"])
        documents.append(document)
        rolled.append({"name": settings.name, "game": settings.game})
    if not documents:
        raise ValueError("There is no player in this file")
    text = "---\n".join(yaml.safe_dump(d, sort_keys=False, allow_unicode=True, default_flow_style=False) for d in documents)
    return {"yaml": text, "rolled": rolled}


# --------------------------------------------------------------------------------------------------------------------
# templates, and the options of every game as a form (what archipelago.gg's player options page shows)
# --------------------------------------------------------------------------------------------------------------------
def cmd_templates(args) -> dict:
    cmd_where(args)
    import yaml
    import worlds  # noqa: F401  (registers all worlds)
    import Options
    from worlds.AutoWorld import AutoWorldRegister
    target = os.path.abspath(args.out)
    templates_error = None
    try:
        Options.generate_yaml_templates(target, generate_hidden=False)
    except Exception as e:  # noqa: BLE001 - a broken world stops the templates after it, but not the forms
        cause = f" ({type(e.__cause__).__name__}: {e.__cause__})" if e.__cause__ else ""
        templates_error = f"{e}{cause}"
        os.makedirs(target, exist_ok=True)
    templates = []
    for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
        if entry.is_file() and entry.name.endswith(".yaml"):
            try:
                with open(entry.path, encoding="utf-8-sig") as f:
                    doc = yaml.safe_load(f) or {}
                game = doc.get("game")
            except Exception:
                game = None
            templates.append({"file": entry.name, "game": game if isinstance(game, str) else None})

    # One file per game: the item and location names alone can be a few hundred kilobytes for a big game.
    forms_dir = os.path.join(target, "forms")
    os.makedirs(forms_dir, exist_ok=True)
    forms, form_errors = {}, {}
    for number, (game, world) in enumerate(sorted(AutoWorldRegister.world_types.items())):
        if world.hidden:
            continue
        try:
            form = option_form(world)
        except Exception as e:  # noqa: BLE001 - a broken custom world must not cost everyone else their forms
            form_errors[game] = f"{type(e).__name__}: {e}"
            continue
        name = f"{number:04d}.json"
        with open(os.path.join(forms_dir, name), "w", encoding="utf-8") as f:
            json.dump(form, f, ensure_ascii=False, separators=(",", ":"))
        forms[game] = name
    return {"templates": templates, "templates_error": templates_error, "forms": forms, "form_errors": form_errors}


def _plain(value):
    """A value as JSON has it. Option keys are nearly always text; anything else becomes text rather than an error."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=str)
    return str(value)


def _keys(keys) -> list:
    """Valid keys in their own order if they have one, else sorted (as the player options page shows them)."""
    import collections.abc
    keys = list(keys) if isinstance(keys, collections.abc.Sequence) else sorted(keys, key=str)
    return [_plain(k) for k in keys]


def option_field(key: str, option, world) -> dict | None:
    """How to show one option in a form, or None if it is not one a form can edit (plando, free dictionaries)."""
    import Options
    from inspect import cleandoc
    rich = option.rich_text_doc if option.rich_text_doc is not None else bool(world.web.rich_text_options_doc)
    field = {"key": key, "label": str(getattr(option, "display_name", None) or key),
             "doc": cleandoc(option.__doc__ or "").strip(), "rich": rich}
    if issubclass(option, Options.Toggle):
        field.update(type="toggle")
    elif issubclass(option, Options.Choice):
        # [name, label, number]; a yaml can also use an alias ("on" for "true") or the number
        choices = [[name, str(option.get_option_name(value)), _plain(value)] for value, name in option.name_lookup.items()
                   if name != "random"]
        aliases = {alias: option.name_lookup[value] for alias, value in option.options.items()
                   if value in option.name_lookup and option.name_lookup[value] != alias}
        if issubclass(option, Options.TextChoice):
            field.update(type="text_choice", choices=sorted(choices), aliases=aliases)
        else:
            field.update(type="choice", choices=choices, aliases=aliases)
    elif issubclass(option, Options.Range):
        field.update(type="range", min=option.range_start, max=option.range_end,
                     named=[[name, value] for name, value in getattr(option, "special_range_names", {}).items()])
    elif issubclass(option, Options.FreeText):
        field.update(type="text")
    elif issubclass(option, Options.OptionCounter):
        if option.valid_keys:
            field.update(keys=_keys(option.valid_keys))
        elif option.verify_item_name or option.verify_location_name:
            field.update(names="items" if option.verify_item_name else "locations")
        else:
            return None
        field.update(type="counter", min=option.min, max=option.max)
    elif issubclass(option, (Options.OptionSet, Options.OptionList)):
        field.update(type="set" if issubclass(option, Options.OptionSet) else "list")
        if option.valid_keys:
            field.update(keys=_keys(option.valid_keys))
        if option.verify_item_name or option.verify_location_name:
            field.update(names="items" if option.verify_item_name else "locations",
                         groups=bool(option.convert_name_groups))
        elif not option.valid_keys and not (isinstance(option.default, (list, tuple, set, frozenset))
                                            and all(isinstance(entry, str) for entry in option.default)):
            return None
    else:
        return None
    # "random" picks one of the choices; for the other kinds it is not a value a form can offer
    field["random"] = bool(option.supports_weighting) and field["type"] in ("toggle", "choice", "text_choice", "range")
    try:
        field["default"] = form_value(option, option.default)
    except Exception:  # noqa: BLE001 - a default Archipelago would only notice when generating with it
        field["default"] = _plain(option.default)
    return field


def form_value(option, raw):
    """A value the way a form holds it: the option's own name for choices, numbers for ranges, lists for sets."""
    import Options
    if issubclass(option, Options.FreeText):  # the text itself; some worlds turn it into a number as they read it
        return raw if isinstance(raw, str) else str(option.from_any(raw).value)
    if raw == "random" or (isinstance(raw, str) and raw.startswith("random") and issubclass(option, Options.Range)):
        return "random"
    value = option.from_any(raw)
    if isinstance(value, Options.Toggle):
        return bool(value.value)
    if isinstance(value, Options.Choice):  # TextChoice too: its own text when that is not one of the choices
        key = value.current_key
        if isinstance(key, str) and option.options.get(key.lower()) in option.name_lookup:
            return option.name_lookup[option.options[key.lower()]]  # "M" or an alias as the choice's own name
        return key
    if isinstance(value, Options.Range):
        return int(value.value)
    if isinstance(value, Options.OptionCounter):
        return {str(k): int(v) for k, v in value.value.items()}
    if isinstance(value, Options.OptionSet):
        return sorted((_plain(v) for v in value.value), key=str)
    return _plain(value.value)


def option_form(world) -> dict:
    """A world's options for the form: in its option groups, with the item and location names some of them pick from."""
    import Options
    collapsed = {group.name: group.start_collapsed for group in world.web.option_groups}
    groups, names = [], set()
    for group, options in Options.get_option_groups(world, visibility_level=Options.Visibility.simple_ui).items():
        fields = []
        for key, option in options.items():
            field = option_field(key, option, world)
            if field:
                fields.append(field)
                if field.get("names"):
                    names.add(field["names"])
        if fields:
            groups.append({"name": group, "collapsed": bool(collapsed.get(group, False)), "options": fields})

    presets = {}
    for preset_name, preset in world.web.options_presets.items():
        values = {}
        for key, raw in preset.items():
            option = world.options_dataclass.type_hints.get(key)
            try:
                values[key] = form_value(option, raw)
            except Exception:  # noqa: BLE001 - a preset value the option does not take; Archipelago would refuse it too
                continue
        presets[str(preset_name)] = values

    hide = {"Everything", "Everywhere"}  # the groups of all items and all locations; picking them is not useful here
    return {
        "game": world.game,
        "version": version_str(getattr(world, "world_version", None)),
        "groups": groups,
        "presets": presets,
        "items": sorted(world.item_names) if "items" in names else [],
        "item_groups": sorted(set(world.item_name_groups) - hide) if "items" in names else [],
        "locations": sorted(world.location_names) if "locations" in names else [],
        "location_groups": sorted(set(world.location_name_groups) - hide) if "locations" in names else [],
    }


# --------------------------------------------------------------------------------------------------------------------
# host.yaml
# --------------------------------------------------------------------------------------------------------------------
def cmd_host_yaml(args) -> dict:
    """Makes sure host.yaml exists (Archipelago creates it on first use) and says where it is."""
    where = cmd_where(args)
    import settings
    settings.skip_autosave = True
    settings.get_settings()
    return {"path": where["host_yaml"]}


def cmd_validate_host_yaml(args) -> dict:
    """Loads a candidate host.yaml with Archipelago's own loader without touching any real file."""
    cmd_where(args)
    import io
    import settings
    settings.skip_autosave = True
    loaded = settings.Settings(args.path)
    loaded.dump(io.StringIO())  # exercises the type conversion of every section
    return {"valid": True}


def cmd_check_rom(args) -> dict:
    """Whether a file is the base ROM a setting asks for, by the setting's own check: its checksums, and for SNES ROMs
    a header that may or may not be there."""
    cmd_where(args)
    import settings
    import worlds  # noqa: F401 - registers the worlds, whose settings name the files
    settings.no_gui = True
    settings.skip_autosave = True
    key, field = args.setting.split(".", 1)
    value = object.__getattribute__(getattr(settings.get_settings(), key), field)
    try:
        type(value).validate(args.path)
    except Exception as e:  # noqa: BLE001 - every way a file can be wrong
        return {"valid": False, "error": str(e).replace(args.path, "the file")}
    return {"valid": True, "error": None}


COMMANDS = {
    "check-rom": cmd_check_rom,
    "host-yaml": cmd_host_yaml,
    "validate-host-yaml": cmd_validate_host_yaml,
    "where": cmd_where,
    "games": cmd_games,
    "multidata": cmd_multidata,
    "progress": cmd_progress,
    "names": cmd_names,
    "tracker": cmd_tracker,
    "build-save": cmd_build_save,
    "roll": cmd_roll,
    "templates": cmd_templates,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ap-dir", required=True, help="Archipelago source directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("where")
    sub.add_parser("games")
    p = sub.add_parser("multidata")
    p.add_argument("path")
    p = sub.add_parser("progress")
    p.add_argument("save")
    p = sub.add_parser("names")
    p.add_argument("path")
    p.add_argument("request", help='JSON: {"items": [[slot, id], ...], "locations": [[slot, id], ...]}')
    p = sub.add_parser("tracker")
    p.add_argument("path", help="the multidata")
    p.add_argument("save", help="the server's save; need not exist yet")
    p.add_argument("slot", type=int)
    p = sub.add_parser("build-save")
    p.add_argument("path", help="the multidata")
    p.add_argument("state", help="JSON file: how the game stands (see cmd_build_save)")
    p.add_argument("out", help="where the save is written")
    p = sub.add_parser("roll")
    p.add_argument("path", help="a player file, perhaps with weights")
    p = sub.add_parser("templates")
    p.add_argument("out")
    sub.add_parser("host-yaml")
    p = sub.add_parser("check-rom")
    p.add_argument("setting", help="the setting in host.yaml, such as lttp_options.rom_file")
    p.add_argument("path")
    p = sub.add_parser("validate-host-yaml")
    p.add_argument("path")
    args = parser.parse_args()

    # setup() changes the working directory, so resolve user supplied paths first
    for attr in ("path", "save", "out", "state"):
        if getattr(args, attr, None):
            setattr(args, attr, os.path.abspath(getattr(args, attr)))
    setup(args.ap_dir)
    try:
        result = COMMANDS[args.command](args)
    except Exception as e:  # noqa: BLE001 - report everything to the caller
        traceback.print_exc(file=sys.stderr)
        fail(f"{type(e).__name__}: {e}")
    emit({"ok": True, **result})


if __name__ == "__main__":
    main()
