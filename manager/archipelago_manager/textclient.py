"""The text client in the browser: the manager connects to a game's server as one of its players.

Like Archipelago's own text client, the connection uses the TextOnly tag: it receives no items and checks no locations,
it only reads the server's messages and says what the player types (chat, or server commands such as !hint). The
browser asks for new messages every second or two and posts what the player types; the manager holds the connection.
That keeps the server's password and port out of the browser, and works through any reverse proxy.

There is one connection per player and slot, shared by all of their tabs. It closes when nobody asked for messages for
a while, and reconnects when the server restarts.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import apbridge, db
from .rooms import ROOMS

log = logging.getLogger(__name__)

IDLE_SECONDS = 90           # without a request for messages, the connection closes
KEEP_MESSAGES = 500
MAX_MESSAGE_LENGTH = 1000
RECONNECT_DELAYS = (2, 5, 10, 30)
MAX_PACKET = 64 * 1024 * 1024  # the data package of a big multiworld is large
COMMAND_TIMEOUT = 20         # to connect and hear back, for the buttons that send a command

# Item flags (NetUtils.ItemClassification): what Archipelago's clients color items by
FLAG_PROGRESSION, FLAG_USEFUL, FLAG_TRAP = 0b001, 0b010, 0b100

# The names of items and locations, by game and data package checksum, shared by all connections
_NAMES: dict[tuple[str, str], tuple[dict[int, str], dict[int, str]]] = {}


class TextClientError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class Refused(Exception):
    pass


@dataclass
class Session:
    game_id: int
    slot: int
    user_id: int
    state: str = "connecting"  # connecting | connected | offline | refused
    error: str | None = None
    seq: int = 0
    messages: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP_MESSAGES))
    last_seen: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    ws: Any = None
    closed: bool = False
    # learned from the server
    team: int = 0
    players: dict[int, str] = field(default_factory=dict)  # slot -> name (alias) in our team
    slot_games: dict[int, str] = field(default_factory=dict)
    names: dict[str, tuple[dict[int, str], dict[int, str]]] = field(default_factory=dict)  # game -> items, locations
    hint_points: int | None = None
    hint_cost_percent: int = 0
    location_count: int = 0

    @property
    def hint_cost(self) -> int:
        """What a hint costs this slot, in points (MultiServer.Context.get_hint_cost)."""
        if not self.hint_cost_percent:
            return 0
        return max(1, int(self.hint_cost_percent * 0.01 * self.location_count))

    def add(self, parts: list[dict], kind: str, mine: bool) -> None:
        self.seq += 1
        self.messages.append({"seq": self.seq, "time": time.time(), "type": kind, "parts": parts, "mine": mine})

    def note(self, text: str) -> None:
        """A line from the manager itself, not from the server."""
        self.add([{"text": text}], "Client", True)


# ----------------------------------------------------------------------------------------------------------------------
# turning the server's messages into text
# ----------------------------------------------------------------------------------------------------------------------
def item_kind(flags: int) -> str:
    if flags & FLAG_PROGRESSION:
        return "progression"
    if flags & FLAG_USEFUL:
        return "useful"
    if flags & FLAG_TRAP:
        return "trap"
    return "filler"


def render(session: Session, data: list[dict]) -> list[dict]:
    """The parts of a PrintJSON message with names instead of ids: [{text, kind?, ...}], as NetUtils.JSONtoTextParser
    reads them. `kind` says how to show a part: player (with `me`), item (with `item`: progression, useful, trap or
    filler), location, entrance, hint_status (with `status`) or color (with `color`)."""
    parts = []
    for node in data:
        if not isinstance(node, dict):
            continue
        kind, text = node.get("type") or "text", str(node.get("text", ""))
        if kind == "player_id":
            slot = _int(text)
            parts.append({"text": session.players.get(slot, f"Player {text}"), "kind": "player",
                          "me": slot == session.slot})
        elif kind == "player_name":
            parts.append({"text": text, "kind": "player", "me": False})
        elif kind in ("item_id", "item_name"):
            if kind == "item_id":
                text = _lookup(session, 0, _int(text), _int(node.get("player")), "Unknown item")
            parts.append({"text": text, "kind": "item", "item": item_kind(_int(node.get("flags")))})
        elif kind in ("location_id", "location_name"):
            if kind == "location_id":
                text = _lookup(session, 1, _int(text), _int(node.get("player")), "Unknown location")
            parts.append({"text": text, "kind": "location"})
        elif kind == "entrance_name":
            parts.append({"text": text, "kind": "entrance"})
        elif kind == "hint_status":
            parts.append({"text": text, "kind": "hint_status", "status": _int(node.get("hint_status"))})
        elif kind == "color":
            parts.append({"text": text, "kind": "color", "color": str(node.get("color", ""))})
        else:
            parts.append({"text": text})
    return parts


def concerns_me(session: Session, packet: dict) -> bool:
    """Whether a message is about this player: their chat, items they send or receive, hints for or in their world,
    and the server's answers to them."""
    kind = packet.get("type")
    if kind in ("CommandResult", "AdminCommandResult", "Tutorial"):
        return True
    if packet.get("slot") == session.slot or packet.get("receiving") == session.slot:
        return True
    item = packet.get("item")
    if isinstance(item, dict) and item.get("player") == session.slot:
        return True
    return any(isinstance(node, dict) and node.get("type") == "player_id" and _int(node.get("text")) == session.slot
               for node in packet.get("data") or [])


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _lookup(session: Session, which: int, key: int, slot: int, fallback: str) -> str:
    # slot 0 is the server: items it hands out (!getitem, starting inventory) belong to the "Archipelago" game
    names = session.names.get(session.slot_games.get(slot) or "Archipelago")
    return names[which].get(key, fallback) if names else fallback


def _server_up(game_id: int) -> bool:
    return ROOMS.status(game_id)["state"] in ("starting", "running")


# ----------------------------------------------------------------------------------------------------------------------
# the connections
# ----------------------------------------------------------------------------------------------------------------------
class TextClients:
    def __init__(self) -> None:
        self.sessions: dict[tuple[int, int, int], Session] = {}

    def session(self, game_id: int, slot: int, user_id: int) -> Session:
        """The player's connection to the slot, started (again) if the server runs and it is not connected."""
        key = (game_id, slot, user_id)
        session = self.sessions.get(key)
        if session is None:
            session = self.sessions[key] = Session(game_id, slot, user_id)
        session.last_seen = time.time()
        if session.task is None or session.task.done():
            if _server_up(game_id):
                session.state, session.error = "connecting", None
                session.task = asyncio.create_task(self._run(session))
            else:
                session.state = "offline"
        return session

    async def say(self, session: Session, text: str) -> None:
        text = text.strip()
        if not text:
            raise TextClientError("Type a message or a command")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise TextClientError(f"A message can be at most {MAX_MESSAGE_LENGTH} characters long")
        if text.startswith("/"):
            raise TextClientError("Commands that start with / belong to Archipelago's own clients. The server's "
                                  "commands start with !, for example !hint or !help.")
        if session.state != "connected" or session.ws is None:
            raise TextClientError("Not connected to the game's server", 409)
        try:
            await session.ws.send(json.dumps([{"cmd": "Say", "text": text}]))
        except WebSocketException:
            raise TextClientError("Not connected to the game's server", 409) from None

    async def command(self, game_id: int, slot: int, user_id: int, text: str, answers: tuple[str, ...]) -> dict | None:
        """Says a command as the player, connecting first if needed. Returns the server's answer: the first message
        about this player of one of the `answers` types (or a CommandResult, which is how the server refuses), or None
        if none came in time."""
        session = self.session(game_id, slot, user_id)
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while session.state == "connecting" and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if session.state == "offline":
            raise TextClientError("The game's server is not running", 409)
        if session.state == "refused":
            raise TextClientError(f"The server refused the connection: {session.error}", 409)
        before = session.seq
        await self.say(session, text)
        while time.monotonic() < deadline:
            for message in list(session.messages):
                if message["seq"] > before and message["mine"] and message["type"] in (*answers, "CommandResult"):
                    return message
            await asyncio.sleep(0.2)
        return None

    async def close(self, key: tuple[int, int, int]) -> None:
        session = self.sessions.pop(key, None)
        if session is None:
            return
        session.closed = True
        if session.ws is not None:
            with contextlib.suppress(Exception):
                await session.ws.close()
        if session.task and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session.task

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(key) for key in list(self.sessions)), return_exceptions=True)

    async def run_periodically(self) -> None:
        """Closes the connections nobody looks at any more."""
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for key, session in list(self.sessions.items()):
                if now - session.last_seen > IDLE_SECONDS:
                    try:
                        await self.close(key)
                    except Exception:  # noqa: BLE001 - keep the others going
                        log.exception("Closing the text client of game %s failed", key[0])

    # ------------------------------------------------------------------------------------------------------------
    async def _run(self, session: Session) -> None:
        """Connects, and connects again after the server restarted or the connection broke, until the session closes
        or the server is stopped."""
        attempt = 0
        while not session.closed:
            if not _server_up(session.game_id):
                session.state = "offline"
                return
            try:
                await self._connect(session)
                attempt = 0
            except Refused as e:
                if session.state != "refused" or session.error != str(e):
                    session.note(f"The server refused the connection: {e}")
                session.state, session.error = "refused", str(e)
                attempt = len(RECONNECT_DELAYS) - 1
            except (OSError, TimeoutError, WebSocketException, ValueError) as e:
                log.debug("Text client of game %s, slot %s: %s", session.game_id, session.slot, e)
            except Exception:  # noqa: BLE001 - a bug must not end the session for good
                log.exception("Text client of game %s, slot %s failed", session.game_id, session.slot)
            finally:
                session.ws = None
            if session.closed:
                return
            if session.state == "connected":
                session.note("The connection to the server was lost.")
            if session.state != "refused":
                session.state = "connecting"
            await asyncio.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
            attempt += 1

    async def _connect(self, session: Session) -> None:
        game = db.query_one("SELECT port, password FROM games WHERE id = ?", (session.game_id,))
        row = db.query_one("SELECT name FROM game_slots WHERE game_id = ? AND slot = ?", (session.game_id, session.slot))
        if not game or not row or not game["port"]:
            raise Refused("The game or the slot no longer exists")
        version = (apbridge.version_tuple() or (0, 6, 0)) + (0, 0, 0)
        # The server is in the same container; no proxy from the environment may come between
        async with connect(f"ws://127.0.0.1:{game['port']}", proxy=None, max_size=MAX_PACKET, open_timeout=15) as ws:
            session.ws = ws
            room = await self._expect(ws, "RoomInfo")
            session.hint_cost_percent = _int(room.get("hint_cost"))
            await self._load_names(session, ws, room)
            await ws.send(json.dumps([{
                "cmd": "Connect", "password": game["password"], "name": row["name"], "game": "",
                "uuid": f"archipelago-manager-{session.game_id}-{session.slot}-{session.user_id}",
                "version": {"major": version[0], "minor": version[1], "build": version[2], "class": "Version"},
                "items_handling": 0, "tags": ["TextOnly"], "slot_data": False}]))
            while not session.closed:
                for packet in json.loads(await ws.recv()):
                    self._handle(session, packet)

    @staticmethod
    async def _expect(ws: Any, cmd: str) -> dict:
        for packet in json.loads(await asyncio.wait_for(ws.recv(), 30)):
            if packet.get("cmd") == cmd:
                return packet
        raise ValueError(f"Expected {cmd} from the server")

    async def _load_names(self, session: Session, ws: Any, room: dict) -> None:
        """Item and location names of every game in the multiworld, before any message that needs them arrives."""
        checksums = room.get("datapackage_checksums") or {}
        games = [g for g in room.get("games") or [] if isinstance(g, str)]
        wanted = [g for g in games if (g, checksums.get(g, "")) not in _NAMES or not checksums.get(g)]
        if wanted:
            await ws.send(json.dumps([{"cmd": "GetDataPackage", "games": wanted}]))
            package = await self._expect(ws, "DataPackage")
            for game, data in (package.get("data", {}).get("games") or {}).items():
                items = {v: k for k, v in (data.get("item_name_to_id") or {}).items()}
                locations = {v: k for k, v in (data.get("location_name_to_id") or {}).items()}
                _NAMES[(game, data.get("checksum") or checksums.get(game, ""))] = (items, locations)
                session.names[game] = (items, locations)
        for game in games:
            if game not in session.names and (game, checksums.get(game, "")) in _NAMES:
                session.names[game] = _NAMES[(game, checksums.get(game, ""))]

    def _handle(self, session: Session, packet: dict) -> None:
        cmd = packet.get("cmd")
        if cmd == "ConnectionRefused":
            raise Refused(", ".join(packet.get("errors") or []) or "no reason given")
        if cmd == "Connected":
            session.team = _int(packet.get("team"))
            self._players(session, packet.get("players"))
            session.slot_games = {_int(k): v.get("game", "") for k, v in (packet.get("slot_info") or {}).items()
                                  if isinstance(v, dict)}
            session.hint_points = packet.get("hint_points")
            session.location_count = len(packet.get("missing_locations") or []) + \
                len(packet.get("checked_locations") or [])
            session.state, session.error = "connected", None
        elif cmd == "RoomUpdate":
            if "hint_points" in packet:
                session.hint_points = packet["hint_points"]
            if "hint_cost" in packet:
                session.hint_cost_percent = _int(packet["hint_cost"])
            if "players" in packet:
                self._players(session, packet["players"])
        elif cmd == "PrintJSON":
            session.add(render(session, packet.get("data") or []), str(packet.get("type") or ""),
                        concerns_me(session, packet))

    @staticmethod
    def _players(session: Session, players: Any) -> None:
        for p in players or []:
            if isinstance(p, dict) and _int(p.get("team")) == session.team:
                session.players[_int(p.get("slot"))] = str(p.get("alias") or p.get("name") or "")


def suggestions(session: Session) -> dict:
    """The player's own items and locations, for typing !hint and !hint_location."""
    items, locations = session.names.get(session.slot_games.get(session.slot, ""), ({}, {}))
    return {"items": sorted(items.values(), key=str.casefold), "locations": sorted(locations.values(), key=str.casefold)}


TEXT_CLIENTS = TextClients()
