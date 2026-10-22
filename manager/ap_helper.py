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
    import Utils
    with open(args.save, "rb") as f:
        save = Utils.restricted_loads(zlib.decompress(f.read()))

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


def cmd_names(args) -> dict:
    """Player, item and location names for ids, from the multidata's own data package (custom games included).

    The request lists [slot, id] pairs: items in the game of the slot receiving them, locations in the game of the
    slot they are in.
    """
    cmd_where(args)
    md = decode_multidata(args.path)
    request = json.loads(args.request)
    package = md.get("datapackage", {})
    reverse: dict[tuple[str, str], dict[int, str]] = {}

    def names_of(kind: str, game: str) -> dict[int, str]:
        if (kind, game) not in reverse:
            mapping = package.get(game, {}).get(f"{kind}_name_to_id", {})
            reverse[kind, game] = {value: name for name, value in mapping.items()}
        return reverse[kind, game]

    def lookup(kind: str, slot: int, ident: int) -> str:
        info = md["slot_info"].get(slot)
        # ids below zero (the server, cheats) belong to the "Archipelago" pseudo game
        for game in ((info.game,) if info else ()) + ("Archipelago",):
            if ident in names_of(kind, game):
                return names_of(kind, game)[ident]
        return f"{kind.capitalize()} {ident}"

    return {
        "players": {int(slot): info.name for slot, info in md["slot_info"].items()},
        "items": [[slot, ident, lookup("item", slot, ident)] for slot, ident in request.get("items", [])],
        "locations": [[slot, ident, lookup("location", slot, ident)] for slot, ident in request.get("locations", [])],
    }


# --------------------------------------------------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------------------------------------------------
def cmd_templates(args) -> dict:
    cmd_where(args)
    import yaml
    import worlds  # noqa: F401  (registers all worlds)
    import Options
    target = os.path.abspath(args.out)
    Options.generate_yaml_templates(target, generate_hidden=False)
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
    return {"templates": templates}


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


COMMANDS = {
    "host-yaml": cmd_host_yaml,
    "validate-host-yaml": cmd_validate_host_yaml,
    "where": cmd_where,
    "games": cmd_games,
    "multidata": cmd_multidata,
    "progress": cmd_progress,
    "names": cmd_names,
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
    p = sub.add_parser("templates")
    p.add_argument("out")
    sub.add_parser("host-yaml")
    p = sub.add_parser("validate-host-yaml")
    p.add_argument("path")
    args = parser.parse_args()

    # setup() changes the working directory, so resolve user supplied paths first
    for attr in ("path", "save", "out"):
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
