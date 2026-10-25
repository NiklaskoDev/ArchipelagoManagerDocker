"""Runs one Archipelago MultiServer process per hosted game.

A game can fall asleep: when nobody was connected to its server for the time set in its options (auto_shutdown_minutes,
0 = never), the manager stops the server and marks the game asleep. A player opening the game page wakes it up again
(POST /api/games/{id}/wake). While asleep, a game keeps its port, unless another game needs one and none is free: then
the port of a game whose server is not running goes to that game, and the first one gets a new port when it starts.
That way there can be more games than ports.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from . import activity, apbridge, backups, db, gamepaths, history, notify, settings as app_settings, versions
from .config import CONFIG
from .history import STATUS_GOAL

log = logging.getLogger(__name__)

LOG_LINES_IN_MEMORY = 2000
LOG_FILE_LIMIT = 5 * 1024 * 1024
STOP_GRACE_SECONDS = 20
READY_TIMEOUT = 90
CRASH_RESTART_WINDOW = 10 * 60
CRASH_RESTART_LIMIT = 3
PROGRESS_CHECK_INTERVAL = 20
IDLE_CHECK_INTERVAL = 10

# A password is handed to the server as a console argument, so it must survive being split on whitespace.
_PASSWORD_OK = re.compile(r"[^\s\x00-\x1f\x7f]{1,64}")


# The server repeats every /option it is given, including the passwords set at each start, and chat may carry an
# "!admin login". Those must not reach the log: players see parts of it (the last line becomes the crash reason), and
# crash reasons go to Discord.
_SECRET_OPTION = re.compile(r"((?:/option|Set option)\s+(?:server_)?password\s+(?:to\s+)?|!admin\s+login\s+)\S+",
                            re.IGNORECASE)


def mask_secrets(line: str) -> str:
    return _SECRET_OPTION.sub(r"\1********", line)


# `/option password <word>` turns these into "no password", which would silently disable the one being set.
_RESERVED_PASSWORDS = {"null", "none", '""', "''"}
# MultiServer logs every goal the moment a client reports it; the save has it up to a minute later
_GOAL_LINE = re.compile(r"Notice \(all\): (.+) \(Team #\d+\) has completed their goal\.$")


def valid_password(value: str) -> bool:
    return bool(_PASSWORD_OK.fullmatch(value)) and value.lower() not in _RESERVED_PASSWORDS


class RoomError(RuntimeError):
    pass


@dataclass
class Room:
    game_id: int
    state: str = "stopped"  # stopped | starting | running | stopping | crashed
    proc: asyncio.subprocess.Process | None = None
    started_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    port: int | None = None
    seq: int = 0
    lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LINES_IN_MEMORY))
    want_stop: bool = False
    crash_times: list[float] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set = field(default_factory=set)
    restart_handle: asyncio.TimerHandle | None = None
    log_loaded: bool = False
    announce: bool = False  # tell Discord once the server accepts connections
    clients: int = 0  # connected now, counted from the joins and leaves in the log
    idle_since: float | None = None  # since when nobody is connected, once the server accepts connections

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "stopping")


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def allocate_port(exclude_game_id: int | None = None) -> int | None:
    """The first port in the configured range that no game claims and nothing is listening on."""
    claimed = {r["port"] for r in db.query("SELECT port FROM games WHERE port IS NOT NULL AND id != ?",
                                           (exclude_game_id or 0,))}
    for port in range(CONFIG.port_start, CONFIG.port_end + 1):
        if port not in claimed and port_is_free(port):
            return port
    return None


def sleep_minutes(game: Any) -> int:
    """After how many minutes with nobody connected the server of a game falls asleep; 0 = never."""
    return app_settings.clean_server_options(db.jloads(game["options"], {}))["auto_shutdown_minutes"]


def _option_commands(game: Any) -> list[str]:
    """`/option` commands that make a freshly started server match the settings of the game.

    The server restores these values from its save file, silently ignoring the command line, so changes made in the
    web UI only take effect if they are re-applied on every start.
    """
    opts = app_settings.clean_server_options(db.jloads(game["options"], {}))
    commands = []
    if not opts["use_embedded_options"]:
        mode = lambda value: value.replace("-", "_")  # noqa: E731 - /option spells it auto_enabled
        commands += [
            f"/option hint_cost {opts['hint_cost']}",
            f"/option location_check_points {opts['location_check_points']}",
            f"/option release_mode {mode(opts['release_mode'])}",
            f"/option collect_mode {mode(opts['collect_mode'])}",
            f"/option remaining_mode {opts['remaining_mode']}",
            f"/option countdown_mode {opts['countdown_mode']}",
            f"/option item_cheat {'off' if opts['disable_item_cheat'] else 'on'}",
            f"/option compatibility {opts['compatibility']}",
        ]
    commands.append(f"/option password {game['password'] or 'null'}")
    commands.append(f"/option server_password {game['server_password'] or 'null'}")
    return commands


def build_command(game: Any) -> list[str]:
    opts = app_settings.clean_server_options(db.jloads(game["options"], {}))
    cmd = apbridge.ap_command(
        "MultiServer.py", str(gamepaths.multidata_path(game["id"])),
        "--host", "0.0.0.0",  # without a host the server tries to look up its public IP on the internet at startup
        "--port", str(game["port"]),
        # in one argument each: a password that starts with "-" would otherwise be read as an option
        f"--password={game['password'] or ''}",
        f"--server_password={game['server_password'] or ''}",
        "--hint_cost", str(opts["hint_cost"]),
        "--location_check_points", str(opts["location_check_points"]),
        "--release_mode", opts["release_mode"],
        "--collect_mode", opts["collect_mode"],
        "--remaining_mode", opts["remaining_mode"],
        "--countdown_mode", opts["countdown_mode"],
        "--auto_shutdown", "0",  # the manager puts the server to sleep itself (watch_idle), also over host.yaml
        "--compatibility", str(opts["compatibility"]),
        "--loglevel", opts["loglevel"],
        version=game["ap_version"],
    )
    if opts["disable_item_cheat"]:
        cmd.append("--disable_item_cheat")
    if opts["use_embedded_options"]:
        cmd.append("--use_embedded_options")
    return cmd


def player_list(game_id: int) -> str:
    """The players of a game as lines for a notification: slot name, game and owner."""
    rows = db.query("SELECT s.name, s.game, u.username FROM game_slots s LEFT JOIN users u ON u.id = s.user_id "
                    "WHERE s.game_id = ? AND s.type = 1 ORDER BY s.slot", (game_id,))
    return "\n".join(f"{notify.md(r['name'])} ({notify.md(r['game'])})"
                     + (f" · {notify.md(r['username'])}" if r["username"] else "") for r in rows)


def slot_owners(game_id: int, slots: set[int] | None = None) -> set[int]:
    """The users who own slots of a game (of these slots, if given), for mentioning them in a notification."""
    rows = db.query("SELECT slot, user_id FROM game_slots WHERE game_id = ? AND user_id IS NOT NULL", (game_id,))
    return {r["user_id"] for r in rows if slots is None or r["slot"] in slots}


def slot_rules(tracker: dict, options: dict) -> dict:
    """What the server's rules give a slot now (MultiServer: get_client_points, get_hint_cost, _cmd_release and
    _cmd_collect): its hint points, what a hint costs, and whether !release and !collect would work. Each of those is
    available, after_goal, automatic (happens by itself at the goal), disabled, or done (nothing left to do)."""
    cost_percent = int(options.get("hint_cost") or 0)
    per_check = int(options.get("location_check_points") or 0)
    total = len(tracker["locations"])
    checked = sum(1 for location in tracker["locations"] if location["checked"])
    cost = max(1, int(cost_percent * 0.01 * total)) if cost_percent else 0

    def rule(mode: str, left: int) -> str:
        if not left:
            return "done"
        if "enabled" in mode:  # enabled and auto-enabled: any time
            return "available"
        if mode == "disabled":
            return "disabled"
        if tracker["goal"]:
            return "available"
        return "automatic" if mode == "auto" else "after_goal"

    return {"hint_points": per_check * checked - cost * int(tracker.get("hints_used") or 0), "hint_cost": cost,
            "points_per_check": per_check,
            "release": rule(str(options.get("release_mode") or "auto"), total - checked),
            "collect": rule(str(options.get("collect_mode") or "auto"), int(tracker.get("items_left") or 0))}


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[int, Room] = {}
        self.closing = False
        self._progress: dict[int, tuple[tuple[float, int], dict]] = {}
        # Per game, what the last look at the save showed: the slots that had reached their goal, and the checked
        # locations per slot. Notifications are about the difference to the next look.
        self._seen: dict[int, tuple[set[int], dict[int, set[int]]]] = {}
        # Per game, the reading of the save last written to the history (history.py): only a new one can change it
        self._recorded: dict[int, dict] = {}
        self._background: set[asyncio.Task] = set()
        self._trackers: dict[tuple[int, int], tuple[tuple[float, int] | None, dict]] = {}
        # one reading of a save at a time per game (and per tracker): the others wait and then share its result
        self._reading: dict[tuple, asyncio.Lock] = {}

    def reset(self) -> None:
        """Forget all in-process state. Only matters when the application starts more than once per process."""
        self.rooms.clear()
        self._progress.clear()
        self._seen.clear()
        self._recorded.clear()
        self._trackers.clear()
        self.closing = False

    def room(self, game_id: int) -> Room:
        return self.rooms.setdefault(game_id, Room(game_id))

    # ------------------------------------------------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------------------------------------------------
    def _load_log_tail(self, room: Room) -> None:
        if room.log_loaded:
            return
        room.log_loaded = True
        path = gamepaths.log_path(room.game_id)
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 64 * 1024))
                tail = f.read().decode(errors="replace").splitlines()[-300:]
        except FileNotFoundError:
            return
        for line in tail:
            room.seq += 1
            room.lines.append((room.seq, line))

    def _append_log(self, room: Room, line: str) -> str:
        """Keeps a line of the server's output, with secrets masked, and returns it as kept."""
        self._load_log_tail(room)  # what came before, from the file, goes first
        line = mask_secrets(line)
        room.seq += 1
        room.lines.append((room.seq, line))
        path = gamepaths.log_path(room.game_id)
        try:
            if path.exists() and path.stat().st_size > LOG_FILE_LIMIT:
                path.replace(path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        return line

    def log_since(self, game_id: int, after: int = 0, limit: int = 500) -> dict:
        room = self.room(game_id)
        self._load_log_tail(room)
        lines = [(seq, text) for seq, text in room.lines if seq > after][-limit:]
        return {"lines": [{"seq": s, "text": t} for s, t in lines], "last": room.seq}

    # ------------------------------------------------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------------------------------------------------
    def status(self, game_id: int) -> dict:
        room = self.room(game_id)
        return {
            "state": room.state,
            "started_at": room.started_at,
            "exit_code": room.exit_code,
            "error": room.error,
        }

    def is_active(self, game_id: int) -> bool:
        return self.room(game_id).active

    # ------------------------------------------------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------------------------------------------------
    async def start(self, game_id: int, *, announce: bool = False) -> None:
        """Starts the server of a game. `announce` sends a Discord message once players can connect."""
        room = self.room(game_id)
        async with room.lock:
            if room.active:
                return
            if room.restart_handle:
                room.restart_handle.cancel()
                room.restart_handle = None
            room.announce = announce
            await self._spawn(room)
        if game_id not in self._seen:
            # What is already in the save is old news. Note it now: the first regular look may come only after a
            # player has finished or found something in this run.
            task = asyncio.create_task(self._note_progress(game_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _spawn(self, room: Room) -> None:
        game = db.query_one("SELECT * FROM games WHERE id = ?", (room.game_id,))
        if not game:
            raise RoomError("No such game")
        if not gamepaths.multidata_path(room.game_id).is_file():
            raise RoomError("The multiworld data file is missing from disk")
        if game["ap_version"] and not versions.available(game["ap_version"]):
            raise RoomError(versions.unavailable_text(game["ap_version"]))
        if game["port"] is None:
            port = allocate_port(room.game_id) or self._take_port(room.game_id)
            if port is None:
                raise RoomError(f"No free port left in {CONFIG.port_start}-{CONFIG.port_end}: the servers of all "
                                f"the other games are running. Stop one of them, or widen PORT_RANGE.")
            db.execute("UPDATE games SET port = ? WHERE id = ?", (port, room.game_id))
            game = db.query_one("SELECT * FROM games WHERE id = ?", (room.game_id,))
        if not port_is_free(game["port"]):
            raise RoomError(f"Port {game['port']} is already in use by something else")

        room.want_stop = False
        room.state, room.error, room.exit_code, room.port = "starting", None, None, game["port"]
        room.started_at = time.time()
        room.clients, room.idle_since = 0, None
        self._load_log_tail(room)
        self._append_log(room, f"--- starting server on port {game['port']} ---")
        try:
            room.proc = await asyncio.create_subprocess_exec(
                *build_command(game),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(apbridge.ap_dir(game["ap_version"])), env=apbridge.ap_env(), start_new_session=True)
        except OSError as e:
            room.state, room.error = "crashed", f"Could not start the server process: {e}"
            self._append_log(room, room.error)
            raise RoomError(room.error) from e

        db.execute("UPDATE games SET desired_state = 'running', asleep = 0 WHERE id = ?", (room.game_id,))
        for command in _option_commands(game):
            self._write_stdin(room, command, echo=False)
        for coro in (self._pump(room, room.proc), self._watch(room, room.proc), self._wait_ready(room, room.proc)):
            task = asyncio.create_task(coro)
            room.tasks.add(task)
            task.add_done_callback(room.tasks.discard)

    def _write_stdin(self, room: Room, text: str, *, echo: bool = True) -> None:
        proc = room.proc
        if not proc or proc.stdin is None or proc.returncode is not None:
            raise RoomError("The server is not running")
        if echo:
            self._append_log(room, f"> {text}")
        proc.stdin.write((text + "\n").encode())

    async def save_soon(self, game_id: int) -> None:
        """Asks a running server to write its save now, so what changed shows up without the minute's wait."""
        room = self.room(game_id)
        if room.state == "running":
            with contextlib.suppress(RoomError, ConnectionError):
                self._write_stdin(room, "/save", echo=False)
                await room.proc.stdin.drain()

    async def send_command(self, game_id: int, text: str) -> None:
        room = self.room(game_id)
        if room.state not in ("starting", "running"):
            raise RoomError("The server is not running")
        text = text.strip()
        if not text or "\n" in text or "\r" in text:
            raise RoomError("Enter a single command")
        self._write_stdin(room, text)
        with contextlib.suppress(ConnectionError):
            await room.proc.stdin.drain()

    async def _pump(self, room: Room, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = self._append_log(room, raw.decode(errors="replace").rstrip("\r\n"))
            if goal := _GOAL_LINE.search(line):
                history.note_goal(room.game_id, goal.group(1), time.time())
            entry = activity.note(room.game_id, line)
            if entry and entry["kind"] in ("join", "part") and room.proc is proc:
                room.clients = max(0, room.clients + (1 if entry["kind"] == "join" else -1))
                room.idle_since = time.time()

    async def _wait_ready(self, room: Room, proc: asyncio.subprocess.Process) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while proc.returncode is None and time.monotonic() < deadline:
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", room.port), 1)
            except (TimeoutError, OSError):
                await asyncio.sleep(0.5)
                continue
            writer.close()
            if room.proc is proc and room.state == "starting":
                room.state = "running"
                room.idle_since = time.time()  # players can connect from now on
                self._append_log(room, f"--- server is accepting connections on port {room.port} ---")
                if room.announce:
                    room.announce = False
                    self._announce_online(room)
            return
        if proc.returncode is None and room.proc is proc and room.state == "starting":
            self._append_log(room, "--- server did not open its port in time; stopping it ---")
            room.error = "The server did not become ready in time"
            await self._terminate(room, proc)

    async def _watch(self, room: Room, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        await asyncio.sleep(0.2)  # let the pump drain the last lines
        if room.proc is not proc:
            return
        room.exit_code = code
        room.clients = 0
        db.execute("UPDATE games SET stopped_at = ? WHERE id = ?", (time.time(), room.game_id))
        self._append_log(room, f"--- server exited with code {code} ---")
        if room.want_stop or self.closing:
            room.state = "stopped"
            return
        if code == 0:
            # The server stopped itself: the /exit console command
            room.state = "stopped"
            db.execute("UPDATE games SET desired_state = 'stopped' WHERE id = ?", (room.game_id,))
            return
        tail = [t for _s, t in list(room.lines)[-8:] if t and not t.startswith("---")]
        room.state = "crashed"
        room.error = room.error or (tail[-1][:300] if tail else f"The server exited with code {code}")
        next_step = self._schedule_restart(room)
        self._announce_crash(room, next_step)

    def _schedule_restart(self, room: Room) -> str:
        """Plans an automatic restart if allowed. Returns what happens next, in words."""
        if not app_settings.get_setting("auto_restart_crashed"):
            return notify.tr("Automatic restarts are turned off.")
        now = time.monotonic()
        room.crash_times = [t for t in room.crash_times if now - t < CRASH_RESTART_WINDOW] + [now]
        if len(room.crash_times) > CRASH_RESTART_LIMIT:
            self._append_log(room, "--- crashed too often, not restarting automatically ---")
            return notify.tr("It crashed too often, so it is not restarted automatically.")
        delay = 5 * 2 ** (len(room.crash_times) - 1)
        self._append_log(room, f"--- restarting in {delay}s (crash {len(room.crash_times)}/{CRASH_RESTART_LIMIT}) ---")

        def restart() -> None:
            room.restart_handle = None
            if not self.closing and room.state == "crashed":
                asyncio.ensure_future(self._restart_after_crash(room))

        room.restart_handle = asyncio.get_running_loop().call_later(delay, restart)
        return notify.tr("Restarting automatically in {delay} seconds (attempt {attempt} of {limit}).", delay=delay,
                         attempt=len(room.crash_times), limit=CRASH_RESTART_LIMIT)

    async def _restart_after_crash(self, room: Room) -> None:
        try:
            await self.start(room.game_id)
        except RoomError as e:
            room.state, room.error = "crashed", str(e)
            self._append_log(room, f"--- restart failed: {e} ---")

    async def _terminate(self, room: Room, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM makes the server save and exit; SIGKILL only if it ignores that."""
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE_SECONDS)
        except TimeoutError:
            self._append_log(room, "--- server ignored the stop request, killing it ---")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def stop(self, game_id: int, *, persist: bool = True) -> None:
        """Stops the server of a game. `persist` remembers that it should stay stopped: not running after a restart of
        the manager, and not woken up by a player (an asleep game is just stopped then)."""
        room = self.room(game_id)
        if room.restart_handle:
            room.restart_handle.cancel()
            room.restart_handle = None
        if persist:
            db.execute("UPDATE games SET desired_state = 'stopped', asleep = 0 WHERE id = ?", (game_id,))
        if not room.active:
            if room.state == "crashed":
                room.state = "stopped"
            return
        room.want_stop = True
        room.state = "stopping"
        proc = room.proc
        assert proc is not None
        await self._terminate(room, proc)
        for task in list(room.tasks):
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2)
        room.state = "stopped"

    async def apply_options(self, game_id: int) -> None:
        """Pushes the current passwords/options of a running server without restarting it."""
        room = self.room(game_id)
        game = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
        if game and room.state in ("starting", "running"):
            for command in _option_commands(game):
                self._write_stdin(room, command, echo=False)
            self._append_log(room, "--- settings updated ---")
            with contextlib.suppress(ConnectionError):
                await room.proc.stdin.drain()

    async def restart(self, game_id: int) -> None:
        await self.stop(game_id)
        await self.start(game_id)

    # ------------------------------------------------------------------------------------------------------------
    # sleeping
    # ------------------------------------------------------------------------------------------------------------
    @staticmethod
    def is_asleep(game: Any) -> bool:
        return bool(game["asleep"]) and game["desired_state"] == "stopped"

    async def wake(self, game_id: int) -> bool:
        """A player opened the game page: an asleep game starts, and a running one stays awake for a while longer.
        Returns whether the server was started."""
        room = self.room(game_id)
        if room.active:
            room.idle_since = time.time()
            return False
        game = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
        # a planned start opens the game at its time, not before
        if not game or not self.is_asleep(game) or (game["start_at"] and game["start_stage"] is None):
            return False
        self._append_log(room, "--- a player opened the game page: waking up ---")
        await self.start(game_id)
        return True

    async def watch_idle(self) -> None:
        """Puts a server to sleep after the time set for its game with nobody connected."""
        minute = CONFIG.sleep_minute_seconds
        while True:
            await asyncio.sleep(min(IDLE_CHECK_INTERVAL, max(1, minute // 4)))
            for game_id, room in list(self.rooms.items()):
                if room.state != "running" or room.clients or room.idle_since is None:
                    continue
                try:
                    game = db.query_one("SELECT options, start_stage FROM games WHERE id = ?", (game_id,))
                    # a game opened ahead of its planned start waits for the players and the countdown
                    if not game or game["start_stage"] == "opened":
                        continue
                    minutes = sleep_minutes(game)
                    if minutes and time.time() - room.idle_since >= minutes * minute:
                        await self._fall_asleep(room, minutes)
                except Exception:  # noqa: BLE001 - one broken game must not keep the others awake
                    log.exception("Putting game %s to sleep failed", game_id)

    async def _fall_asleep(self, room: Room, minutes: int) -> None:
        self._append_log(room, f"--- nobody was connected for {minutes} min: the server goes to sleep until a player "
                               f"opens the game page ---")
        await self.stop(room.game_id, persist=False)
        # unless someone stopped or started it meanwhile
        if room.state == "stopped":
            db.execute("UPDATE games SET desired_state = 'stopped', asleep = 1 WHERE id = ? AND desired_state = 'running'",
                       (room.game_id,))

    def _take_port(self, game_id: int) -> int | None:
        """When no port is free: the port of another game whose server is not running, and is not about to start.
        Finished games give theirs first, then stopped ones, then asleep ones, each the one that stopped longest ago.
        That game gets a new port when it starts again."""
        for row in db.query("SELECT id, port FROM games WHERE id != ? AND port BETWEEN ? AND ? "
                            "AND desired_state = 'stopped' ORDER BY finished_at IS NULL, asleep, "
                            "COALESCE(stopped_at, 0), id", (game_id, CONFIG.port_start, CONFIG.port_end)):
            other = self.rooms.get(row["id"])
            if other and (other.active or other.restart_handle):
                continue
            if not port_is_free(row["port"]):
                continue
            db.execute("UPDATE games SET port = NULL WHERE id = ?", (row["id"],))
            if other:
                other.port = None
            self._append_log(self.room(row["id"]), f"--- port {row['port']} went to another game, as no other port "
                                                   f"was free; this game gets a new one when it starts ---")
            log.info("Game %s took port %s from game %s", game_id, row["port"], row["id"])
            return row["port"]
        return None

    # ------------------------------------------------------------------------------------------------------------
    # manager lifecycle
    # ------------------------------------------------------------------------------------------------------------
    async def start_saved(self) -> None:
        """After a container restart, bring back every game that was running."""
        for row in db.query("SELECT id FROM games WHERE desired_state = 'running' ORDER BY id"):
            try:
                await self.start(row["id"])
            except RoomError as e:
                log.error("Could not restart game %s: %s", row["id"], e)
                room = self.room(row["id"])
                room.state, room.error = "crashed", str(e)

    async def shutdown(self) -> None:
        """Stops every server in parallel but remembers they were running, so they come back on the next start."""
        self.closing = True
        await asyncio.gather(*(self.stop(gid, persist=False) for gid, room in list(self.rooms.items()) if room.active),
                             return_exceptions=True)

    # ------------------------------------------------------------------------------------------------------------
    # progress (read from the server's save file)
    # ------------------------------------------------------------------------------------------------------------
    async def progress(self, game_id: int) -> dict[int, dict]:
        state = await self._read_save(game_id)
        if state is None:
            cached = self._progress.get(game_id)
            return cached[1]["slots"] if cached else {}
        return state["slots"]

    async def _read_save(self, game_id: int) -> dict | None:
        """What the server's save says: progress per slot, the hints and the checked locations per slot. Empty before
        the first save, None if the save could not be read."""
        async with self._reading.setdefault(("save", game_id), asyncio.Lock()):
            return await self._read_save_now(game_id)

    async def _read_save_now(self, game_id: int) -> dict | None:
        path = gamepaths.save_path(game_id)
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._progress.pop(game_id, None)
            return {"slots": {}, "hints": [], "checks": {}}
        key = (stat.st_mtime, stat.st_size)
        cached = self._progress.get(game_id)
        if cached and cached[0] == key:
            return cached[1]
        try:
            result = await apbridge.run_helper("progress", str(path), timeout=60,
                                               version=apbridge.game_version(game_id))
        except apbridge.APBridgeError as e:
            log.warning("Could not read progress of game %s: %s", game_id, e)
            return None
        state = {"slots": {int(k): v for k, v in result["slots"].items()},
                 "hints": [tuple(h) for h in result.get("hints", [])],
                 "checks": {int(k): set(v) for k, v in result.get("checks", {}).items()}}
        self._progress[game_id] = (key, state)
        return state

    def forget_progress(self, game_id: int) -> None:
        """For a save that was replaced or removed: read it afresh, and take it as the new starting point."""
        self._progress.pop(game_id, None)
        self._seen.pop(game_id, None)
        self._recorded.pop(game_id, None)
        for key in [k for k in self._trackers if k[0] == game_id]:
            del self._trackers[key]

    async def tracker(self, game_id: int, slot: int) -> dict:
        """One slot's items, locations and hints with names (see ap_helper.cmd_tracker). Read again only when the save
        changed. Raises apbridge.APBridgeError if Archipelago cannot read the game."""
        async with self._reading.setdefault(("tracker", game_id, slot), asyncio.Lock()):
            return await self._tracker_now(game_id, slot)

    async def _tracker_now(self, game_id: int, slot: int) -> dict:
        path = gamepaths.save_path(game_id)
        try:
            stat = path.stat()
            key: tuple[float, int] | None = (stat.st_mtime, stat.st_size)
        except FileNotFoundError:
            key = None
        cached = self._trackers.get((game_id, slot))
        if cached and cached[0] == key:
            return cached[1]
        data = await apbridge.run_helper("tracker", str(gamepaths.multidata_path(game_id)), str(path), str(slot),
                                         timeout=60, version=apbridge.game_version(game_id))
        data.pop("ok", None)
        self._trackers[game_id, slot] = (key, data)
        return data

    def forget(self, game_id: int) -> None:
        self.rooms.pop(game_id, None)
        activity.forget(game_id)
        for key in [k for k in self._reading if k[1] == game_id]:
            del self._reading[key]
        self._progress.pop(game_id, None)
        self._seen.pop(game_id, None)
        self._recorded.pop(game_id, None)
        for key in [k for k in self._trackers if k[0] == game_id]:
            del self._trackers[key]

    # ------------------------------------------------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------------------------------------------------
    def _announce_online(self, room: Room) -> None:
        game = db.query_one("SELECT name, password, start_at FROM games WHERE id = ?", (room.game_id,))
        if not game:
            return
        address = notify.game_address(room.port)
        fields = [(notify.tr("Server"), f"`{address}`" if address else f"Port {room.port}", True)]
        if game["password"]:
            fields.append((notify.tr("Password"), notify.tr("Shown on the game page"), True))
        if game["start_at"] and game["start_at"] > time.time():  # opened ahead of its planned start
            fields.append((notify.tr("Start"), notify.timestamp(game["start_at"], "R"), True))
        fields.append((notify.tr("Players"), player_list(room.game_id)))
        notify.emit("game_online", notify.tr("{game} is online", game=game["name"]),
                    url=notify.link(f"/games/{room.game_id}"), fields=fields, color="success",
                    ping="game_online", ping_users=slot_owners(room.game_id), game_id=room.game_id)

    def _announce_crash(self, room: Room, next_step: str) -> None:
        game = db.query_one("SELECT name FROM games WHERE id = ?", (room.game_id,))
        if game:
            notify.emit("game_crashed", notify.tr("{game} crashed", game=game["name"]),
                        notify.tr("The server stopped unexpectedly (exit code {code}).\n```\n{error}\n```\n{next_step}",
                                  code=room.exit_code, error=room.error, next_step=next_step),
                        url=notify.link(f"/games/{room.game_id}"), color="error", game_id=room.game_id)

    async def watch_progress(self) -> None:
        """Announces goals and found hinted items. The server writes both to its save, at the latest a minute later."""
        while True:
            await asyncio.sleep(PROGRESS_CHECK_INTERVAL)
            for game_id, room in list(self.rooms.items()):
                if room.started_at is None:  # did not run since the manager started, so its save cannot have changed
                    continue
                try:
                    await self._check_progress(game_id)
                    backups.maybe_auto(game_id)
                except Exception:  # noqa: BLE001 - one broken game must not stop the others
                    log.exception("Checking the progress of game %s failed", game_id)

    @staticmethod
    def _summary(state: dict) -> tuple[set[int], dict[int, set[int]]]:
        return {slot for slot, p in state["slots"].items() if p.get("status") == STATUS_GOAL}, state["checks"]

    async def _note_progress(self, game_id: int) -> None:
        state = await self._read_save(game_id)
        if state is not None and game_id not in self._seen:
            self._seen[game_id] = self._summary(state)
            self._record(game_id, state)

    def _record(self, game_id: int, state: dict) -> None:
        # a save that did not change is read from the cache as the same object
        if self._recorded.get(game_id) is not state:
            history.record(game_id, state)
            self._recorded[game_id] = state

    async def catch_up_history(self) -> None:
        """Notes where games stand that have a save but no history yet: from before the manager kept one, or uploaded
        with progress. Finished ones go to the archive."""
        for row in db.query("SELECT id FROM games g WHERE NOT EXISTS "
                            "(SELECT 1 FROM game_progress p WHERE p.game_id = g.id) ORDER BY id"):
            if not gamepaths.save_path(row["id"]).is_file():
                continue
            try:
                state = await self._read_save(row["id"])
                if state is not None:
                    self._record(row["id"], state)
            except Exception:  # noqa: BLE001 - one broken save must not stop the others
                log.exception("Reading the progress of game %s failed", row["id"])

    async def _check_progress(self, game_id: int) -> None:
        state = await self._read_save(game_id)
        if state is None:
            return
        self._record(game_id, state)
        seen = self._seen.get(game_id)
        self._seen[game_id] = self._summary(state)
        if seen is None:  # the first look is the starting point: what came before was announced back then
            return
        game = db.query_one("SELECT name FROM games WHERE id = ?", (game_id,))
        if not game:
            return
        await self._announce_found_hints(game_id, game["name"], state, seen[1])
        self._announce_goals(game_id, game["name"], self._seen[game_id][0], seen[0])

    async def _announce_found_hints(self, game_id: int, game_name: str, state: dict,
                                    checked_before: dict[int, set[int]]) -> None:
        # A hint is (receiving slot, finding slot, location, item, entrance, item flags). It counts as found now if its
        # location was checked since the last look; hints given for locations checked before are not news.
        found = sorted((h for h in state["hints"] if h[2] in state["checks"].get(h[1], ())
                        and h[2] not in checked_before.get(h[1], ())), key=lambda h: (h[1], h[2]))
        if not found or not notify.wanted("hint_found", game_id):
            return
        request = {"items": [[h[0], h[3]] for h in found], "locations": [[h[1], h[2]] for h in found]}
        try:
            names = await apbridge.run_helper("names", str(gamepaths.multidata_path(game_id)), json.dumps(request),
                                              timeout=60, version=apbridge.game_version(game_id))
        except apbridge.APBridgeError as e:
            log.warning("Could not look up the names of found hints in game %s: %s", game_id, e)
            return
        players = {int(slot): name for slot, name in names["players"].items()}
        items = {(slot, ident): name for slot, ident, name in names["items"]}
        locations = {(slot, ident): name for slot, ident, name in names["locations"]}
        lines, item_names = [], []
        for receiving, finding, location, item, entrance, _flags in found:
            finder = notify.md(players[finding] if finding in players else notify.tr("Player {slot}", slot=finding))
            receiver = players.get(receiving, "")
            item_name = items[receiving, item]
            item_names.append(item_name)
            line = notify.tr("**{finder}** found their **{item}** at {location}" if finding == receiving
                             else "**{finder}** found **{receiver}**'s **{item}** at {location}",
                             finder=finder, receiver=notify.md(receiver), item=notify.md(item_name),
                             location=notify.md(locations[finding, location]))
            lines.append(line + (f" ({notify.md(entrance)})" if entrance else ""))
        url = notify.link(f"/games/{game_id}")
        receivers = slot_owners(game_id, {h[0] for h in found})  # whoever the items are for
        if len(found) == 1:
            notify.emit("hint_found", notify.tr("Hinted item found: {item}", item=item_names[0]),
                        notify.tr("{line} in {game}.", line=lines[0], game=notify.md(game_name)), url=url,
                        color="success", ping="hint_found", ping_users=receivers, game_id=game_id)
            return
        # A !release or !collect can find many hinted items at once: one message instead of a flood
        text, shown = "", 0
        for line in lines:
            if len(text) + len(line) > 3800:
                break
            text, shown = text + line + "\n", shown + 1
        if shown < len(lines):
            text += notify.tr("…and {n} more", n=len(lines) - shown)
        notify.emit("hint_found", notify.tr("{n} hinted items found in {game}", n=len(found), game=game_name),
                    text.strip(), url=url, color="success", ping="hint_found", ping_users=receivers,
                    game_id=game_id)

    def _announce_goals(self, game_id: int, game_name: str, done: set[int], known: set[int]) -> None:
        new = done - known
        if not new:
            return
        players = {r["slot"]: r for r in db.query(
            "SELECT s.slot, s.name, s.game, s.goal_at, s.goal_logged_at, u.username FROM game_slots s "
            "LEFT JOIN users u ON u.id = s.user_id WHERE s.game_id = ? AND s.type = 1", (game_id,))}
        if not players:
            return
        game = db.query_one("SELECT play_started_at, finished_at, start_at, start_stage FROM games WHERE id = ?",
                            (game_id,))
        # a race is timed from its planned start, to the second; other games from their first check
        race = game["start_at"] if game and game["start_at"] and game["start_stage"] == "started" else None
        finished = len(players.keys() & done)
        url = notify.link(f"/games/{game_id}")
        for slot in sorted(new & players.keys()):
            p = players[slot]
            who = notify.md(p["name"]) + (f" ({notify.md(p['username'])})" if p["username"] else "")
            fields = [(notify.tr("Done"), notify.tr("{finished} of {total} players", finished=finished,
                                                    total=len(players)), True)]
            reached = history.goal_time(p)
            if race and reached and reached >= race:
                fields.append((notify.tr("Time"), notify.race_time(reached - race), True))
            notify.emit("player_goal", notify.tr("{player} reached their goal", player=p["name"]),
                        notify.tr("{who} finished **{game}** in {name}.", who=who, game=notify.md(p["game"]),
                                  name=notify.md(game_name)),
                        url=url, fields=fields, color="success", game_id=game_id)
        if players.keys() <= done and not players.keys() <= known:
            fields = None
            last = max((history.goal_time(p) or 0) for p in players.values())
            if race and last >= race:
                fields = [(notify.tr("Time"), notify.race_time(last - race), True)]
            elif game and game["play_started_at"] and game["finished_at"]:
                fields = [(notify.tr("Time"), notify.duration(game["finished_at"] - game["play_started_at"]), True)]
            notify.emit("player_goal", notify.tr("Everyone finished {game}!", game=game_name),
                        notify.tr("All {n} players reached their goal. Congratulations!", n=len(players)), url=url,
                        fields=fields, color="success", game_id=game_id)

ROOMS = RoomManager()
