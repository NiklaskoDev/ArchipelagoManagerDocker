"""Taking a game over from archipelago.gg, with how far everyone got.

archipelago.gg never hands out a room's multiworld or its save. So the game is added here as usual, from the zip the
generator made, and its progress is read from the room's public tracker API: the locations each slot checked, the
items it received, the hints, aliases, who reached their goal, and when everyone last played. The room has to be this
very multiworld: the same slots with the same games and as many locations each. Archipelago's own types turn that into
a save (ap_helper.py build-save), which the server loads like its own. Patch files the game has no file for yet are
downloaded from the room.

What the tracker API does not tell stays behind: the hint points spent (everyone can ask for hints again as if they had
spent none) and the data storage some games keep on the server. The tracker is up to a minute behind the room.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import re
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, apbridge, backups, db, gamepaths, gamestore, history
from .config import CONFIG
from .rooms import ROOMS

log = logging.getLogger(__name__)

ROOM_ID = re.compile(r"[A-Za-z0-9_-]{22}")  # a room's UUID, base64 without padding, as archipelago.gg shows it
MAX_JSON = 64 * 1024 * 1024
MAX_PATCH = 128 * 1024 * 1024
_FILENAME = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.IGNORECASE)


class GGImportError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


def room_id(text: str) -> str:
    """The room from what people paste: the room's address on archipelago.gg, or just its id."""
    value = (text or "").strip()
    if ROOM_ID.fullmatch(value):
        return value
    parts = urllib.parse.urlsplit(value if "://" in value else f"https://{value}")
    host = urllib.parse.urlsplit(CONFIG.archipelago_gg).netloc.lower()
    match = re.fullmatch(r"/room/([A-Za-z0-9_-]{22})/?", parts.path)
    if parts.netloc.lower() not in (host, f"www.{host}") or not match:
        raise GGImportError("Enter the address of the room on archipelago.gg, such as "
                            "https://archipelago.gg/room/AbCdEfGhIjKlMnOpQrStUv")
    return match[1]


def _open(path: str, limit: int) -> tuple[bytes, str]:
    request = urllib.request.Request(f"{CONFIG.archipelago_gg}{path}",
                                     headers={"User-Agent": f"archipelago-manager/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise GGImportError("archipelago.gg sent more than expected", 502)
            return data, response.headers.get("Content-Disposition", "")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GGImportError("There is no such room on archipelago.gg", 404) from None
        raise GGImportError(f"archipelago.gg answered {e.code}", 502) from None
    except (urllib.error.URLError, OSError) as e:
        raise GGImportError(f"Could not reach archipelago.gg: {getattr(e, 'reason', e)}", 502) from None


def _json(path: str) -> dict:
    data, _disposition = _open(path, MAX_JSON)
    try:
        value = json.loads(data)
    except ValueError:
        raise GGImportError("archipelago.gg did not answer with what its API returns", 502) from None
    if not isinstance(value, dict):
        raise GGImportError("archipelago.gg did not answer with what its API returns", 502)
    return value


def _timestamp(value) -> float | None:
    """A time as archipelago.gg's API writes it (an HTTP date), or as a number or ISO date."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def fetch(room: str) -> dict:
    """What archipelago.gg tells about a room: its slots, its patch files and how the game stands (team 0)."""
    status = _json(f"/api/room_status/{room}")
    tracker = status.get("tracker")
    if not isinstance(tracker, str) or not ROOM_ID.fullmatch(tracker):
        raise GGImportError("archipelago.gg did not say where the room's tracker is", 502)
    live = _json(f"/api/tracker/{tracker}")
    static = _json(f"/api/static_tracker/{tracker}")

    def per_slot(entries, key) -> dict[int, object]:
        return {int(e["player"]): e[key] for e in entries or () if isinstance(e, dict) and e.get("team", 0) == 0}

    try:
        return {
            "players": [(str(name), str(game)) for name, game in status.get("players") or ()],
            "downloads": {int(d["slot"]): str(d["download"]) for d in status.get("downloads") or ()},
            "totals": per_slot(static.get("player_locations_total"), "total_locations"),
            "state": {
                "location_checks": per_slot(live.get("player_checks_done"), "locations"),
                "received_items": per_slot(live.get("player_items_received"), "items"),
                "hints": per_slot(live.get("hints"), "hints"),
                "aliases": per_slot(live.get("aliases"), "alias"),
                "client_game_state": per_slot(live.get("player_status"), "status"),
                "client_activity_timers": {slot: _timestamp(t) for slot, t in
                                           per_slot(live.get("activity_timers"), "time").items()},
                "client_connection_timers": {slot: _timestamp(t) for slot, t in
                                             per_slot(live.get("connection_timers"), "time").items()},
            },
        }
    except (KeyError, TypeError, ValueError):
        raise GGImportError("archipelago.gg did not answer with what its API returns", 502) from None


def check_match(game_id: int, found: dict) -> None:
    """Refuses a room that is not this multiworld: other slot names or games, or other numbers of locations."""
    slots = db.query("SELECT slot, name, game, locations FROM game_slots WHERE game_id = ? AND type = 1 ORDER BY slot",
                     (game_id,))
    if [(s["name"], s["game"]) for s in slots] != found["players"]:
        raise GGImportError("That room holds another multiworld: its players are not this game's", 409)
    for s in slots:
        total = found["totals"].get(s["slot"])
        if total is not None and int(total) != s["locations"]:
            raise GGImportError("That room holds another multiworld: the players have other locations", 409)


def _download_patch(game_id: int, slot: int, path: str) -> bool:
    """A slot's patch file from the room. False if archipelago.gg had none to give."""
    if not path.startswith("/"):
        return False
    data, disposition = _open(path, MAX_PATCH)
    match = _FILENAME.search(disposition)
    if not match or not data:
        return False  # the room's page says "Patch not found" or the like instead of sending a file
    with tempfile.NamedTemporaryFile(dir=CONFIG.tmp_dir, delete=False) as tmp:
        tmp.write(data)
    gamestore.add_file(game_id, urllib.parse.unquote(match[1]).replace("/", "_"), Path(tmp.name), kind="patch", slot=slot)
    return True


async def take_over(game_id: int, text: str) -> dict:
    """Replaces the game's progress with the room's (after a backup of it), and adds the patch files it lacks."""
    room = room_id(text)
    if ROOMS.is_active(game_id):
        raise GGImportError("Stop the server before taking over the progress", 409)
    found = await asyncio.to_thread(fetch, room)
    check_match(game_id, found)
    with tempfile.TemporaryDirectory(dir=CONFIG.tmp_dir) as tmp:
        request, built = Path(tmp) / "state.json", Path(tmp) / "save.apsave"
        request.write_text(json.dumps(found["state"]), encoding="utf-8")
        try:
            counts = await apbridge.run_helper("build-save", str(gamepaths.multidata_path(game_id)), str(request),
                                               str(built), timeout=120, version=apbridge.game_version(game_id))
        except apbridge.APBridgeError as e:
            if "lie elsewhere" in str(e):  # ap_helper.cmd_build_save checks every item and hint against the multiworld
                raise GGImportError("That room holds another multiworld: its items lie elsewhere", 409) from None
            raise GGImportError(f"The progress does not fit this multiworld: {e}", 409) from None
        if ROOMS.is_active(game_id):  # started meanwhile
            raise GGImportError("Stop the server before taking over the progress", 409)
        backups.create(game_id, "before-import")
        built.replace(gamepaths.save_path(game_id))
    ROOMS.forget_progress(game_id)
    history.forget(game_id)
    state = await ROOMS.save_state(game_id)  # where everyone stands now, as the first point of the history
    if state is not None:
        history.record(game_id, state)
    # the game began when the first player connected to the room, as far as archipelago.gg remembers
    began = [t for t in found["state"]["client_connection_timers"].values() if t]
    if began:
        db.execute("UPDATE games SET play_started_at = ? WHERE id = ? AND play_started_at IS NULL", (min(began), game_id))
    with_files = {r["slot"] for r in db.query("SELECT DISTINCT slot FROM game_files WHERE game_id = ? AND kind = 'patch' "
                                              "AND slot IS NOT NULL", (game_id,))}
    patches = 0
    for slot, path in found["downloads"].items():
        if slot in with_files or not db.query_one("SELECT 1 FROM game_slots WHERE game_id = ? AND slot = ?",
                                                  (game_id, slot)):
            continue
        try:
            patches += await asyncio.to_thread(_download_patch, game_id, slot, path)
        except (GGImportError, OSError, sqlite3.Error) as e:
            log.warning("Could not download the patch file of slot %s from archipelago.gg: %s", slot, e)
    return {"room": room, "checked": counts["checked"], "received": counts["received"], "hints": counts["hints"],
            "patches": patches}
