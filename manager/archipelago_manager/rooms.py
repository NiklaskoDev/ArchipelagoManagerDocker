"""Runs one Archipelago MultiServer process per hosted game."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from . import apbridge, db, gamepaths, settings as app_settings
from .config import CONFIG

log = logging.getLogger(__name__)

LOG_LINES_IN_MEMORY = 2000
LOG_FILE_LIMIT = 5 * 1024 * 1024
STOP_GRACE_SECONDS = 20
READY_TIMEOUT = 90
CRASH_RESTART_WINDOW = 10 * 60
CRASH_RESTART_LIMIT = 3

# A password is handed to the server as a console argument, so it must survive being split on whitespace.
_PASSWORD_OK = re.compile(r"^[^\s\x00-\x1f\x7f]{1,64}$")


# `/option password <word>` turns these into "no password", which would silently disable the one being set.
_RESERVED_PASSWORDS = {"null", "none", '""', "''"}


def valid_password(value: str) -> bool:
    return bool(_PASSWORD_OK.match(value)) and value.lower() not in _RESERVED_PASSWORDS


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
        "--password", game["password"] or "",
        "--server_password", game["server_password"] or "",
        "--hint_cost", str(opts["hint_cost"]),
        "--location_check_points", str(opts["location_check_points"]),
        "--release_mode", opts["release_mode"],
        "--collect_mode", opts["collect_mode"],
        "--remaining_mode", opts["remaining_mode"],
        "--countdown_mode", opts["countdown_mode"],
        "--auto_shutdown", str(opts["auto_shutdown_minutes"] * 60),
        "--compatibility", str(opts["compatibility"]),
        "--loglevel", opts["loglevel"],
    )
    if opts["disable_item_cheat"]:
        cmd.append("--disable_item_cheat")
    if opts["use_embedded_options"]:
        cmd.append("--use_embedded_options")
    return cmd


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[int, Room] = {}
        self.closing = False
        self._progress: dict[int, tuple[tuple[float, int], dict]] = {}

    def reset(self) -> None:
        """Forget all in-process state. Only matters when the application starts more than once per process."""
        self.rooms.clear()
        self._progress.clear()
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

    def _append_log(self, room: Room, line: str) -> None:
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
    async def start(self, game_id: int) -> None:
        room = self.room(game_id)
        async with room.lock:
            if room.active:
                return
            if room.restart_handle:
                room.restart_handle.cancel()
                room.restart_handle = None
            await self._spawn(room)

    async def _spawn(self, room: Room) -> None:
        game = db.query_one("SELECT * FROM games WHERE id = ?", (room.game_id,))
        if not game:
            raise RoomError("No such game")
        if not gamepaths.multidata_path(room.game_id).is_file():
            raise RoomError("The multiworld data file is missing from disk")
        if game["port"] is None:
            port = allocate_port(room.game_id)
            if port is None:
                raise RoomError(f"No free port left in {CONFIG.port_start}-{CONFIG.port_end}. "
                                f"Stop or delete another game, or widen PORT_RANGE.")
            db.execute("UPDATE games SET port = ? WHERE id = ?", (port, room.game_id))
            game = db.query_one("SELECT * FROM games WHERE id = ?", (room.game_id,))
        if not port_is_free(game["port"]):
            raise RoomError(f"Port {game['port']} is already in use by something else")

        room.want_stop = False
        room.state, room.error, room.exit_code, room.port = "starting", None, None, game["port"]
        room.started_at = time.time()
        self._load_log_tail(room)
        self._append_log(room, f"--- starting server on port {game['port']} ---")
        try:
            room.proc = await asyncio.create_subprocess_exec(
                *build_command(game),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(CONFIG.ap_dir), env=apbridge.ap_env(), start_new_session=True)
        except OSError as e:
            room.state, room.error = "crashed", f"Could not start the server process: {e}"
            self._append_log(room, room.error)
            raise RoomError(room.error) from e

        db.execute("UPDATE games SET desired_state = 'running' WHERE id = ?", (room.game_id,))
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
            self._append_log(room, raw.decode(errors="replace").rstrip("\r\n"))

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
                self._append_log(room, f"--- server is accepting connections on port {room.port} ---")
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
        self._append_log(room, f"--- server exited with code {code} ---")
        if room.want_stop or self.closing:
            room.state = "stopped"
            return
        if code == 0:
            # The server decided to stop on its own: idle auto-shutdown or the /exit console command.
            room.state = "stopped"
            db.execute("UPDATE games SET desired_state = 'stopped' WHERE id = ?", (room.game_id,))
            return
        tail = [t for _s, t in list(room.lines)[-8:] if t and not t.startswith("---")]
        room.state = "crashed"
        room.error = room.error or (tail[-1][:300] if tail else f"The server exited with code {code}")
        self._schedule_restart(room)

    def _schedule_restart(self, room: Room) -> None:
        if not app_settings.get_setting("auto_restart_crashed"):
            return
        now = time.monotonic()
        room.crash_times = [t for t in room.crash_times if now - t < CRASH_RESTART_WINDOW] + [now]
        if len(room.crash_times) > CRASH_RESTART_LIMIT:
            self._append_log(room, "--- crashed too often, not restarting automatically ---")
            return
        delay = 5 * 2 ** (len(room.crash_times) - 1)
        self._append_log(room, f"--- restarting in {delay}s (crash {len(room.crash_times)}/{CRASH_RESTART_LIMIT}) ---")

        def restart() -> None:
            room.restart_handle = None
            if not self.closing and room.state == "crashed":
                asyncio.ensure_future(self._restart_after_crash(room))

        room.restart_handle = asyncio.get_running_loop().call_later(delay, restart)

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
        room = self.room(game_id)
        if room.restart_handle:
            room.restart_handle.cancel()
            room.restart_handle = None
        if persist:
            db.execute("UPDATE games SET desired_state = 'stopped' WHERE id = ?", (game_id,))
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
        path = gamepaths.save_path(game_id)
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._progress.pop(game_id, None)
            return {}
        key = (stat.st_mtime, stat.st_size)
        cached = self._progress.get(game_id)
        if cached and cached[0] == key:
            return cached[1]
        try:
            result = await apbridge.run_helper("progress", str(path), timeout=60)
        except apbridge.APBridgeError as e:
            log.warning("Could not read progress of game %s: %s", game_id, e)
            return cached[1] if cached else {}
        slots = {int(k): v for k, v in result["slots"].items()}
        self._progress[game_id] = (key, slots)
        return slots

    def forget_progress(self, game_id: int) -> None:
        self._progress.pop(game_id, None)

    def forget(self, game_id: int) -> None:
        self.rooms.pop(game_id, None)
        self._progress.pop(game_id, None)


ROOMS = RoomManager()
