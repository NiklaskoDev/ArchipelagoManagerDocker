"""Generating multiworlds with Archipelago's Generate.py, one job at a time."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import logging
import os
import re
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import apbridge, configstore, db, gamestore, notify, yamlinfo
from .config import CONFIG
from .rooms import ROOMS, RoomError, player_list, slot_owners

log = logging.getLogger(__name__)

PLANDO_OPTIONS = ("bosses", "items", "texts", "connections")
KEEP_FINISHED_JOBS = 50
CANCEL_GRACE_SECONDS = 10


class JobRequestError(Exception):
    def __init__(self, message: str, status: int = 400, missing_games: list[str] | None = None) -> None:
        super().__init__(message)
        self.message, self.status, self.missing_games = message, status, missing_games or []


@dataclass
class PreparedPlayer:
    config_id: int | None
    user_id: int | None
    label: str  # the config's name, for messages
    filename: str
    data: bytes
    info: yamlinfo.YamlInfo


def job_dir(job_id: int) -> Path:
    return CONFIG.jobs_dir / str(job_id)


def prepare_players(config_ids: list[int], user: sqlite3.Row, *, is_admin: bool) -> list[PreparedPlayer]:
    """Loads and validates the chosen configs. Raises JobRequestError with a message fit for the user."""
    if not config_ids:
        raise JobRequestError("Select at least one player config")
    if len(set(config_ids)) != len(config_ids):
        raise JobRequestError("The same config was selected twice")
    if len(config_ids) > 500:
        raise JobRequestError("Too many players")

    players: list[PreparedPlayer] = []
    used_filenames: set[str] = set()
    for config_id in config_ids:
        row = db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,))
        if not row or (row["user_id"] != user["id"] and not is_admin):
            raise JobRequestError(f"Config {config_id} does not exist", 404)
        try:
            data = configstore.config_path(row["user_id"], row["id"]).read_bytes()
        except FileNotFoundError:
            raise JobRequestError(f"The file for '{row['name']}' is missing from disk", 410) from None
        players.append(prepare_player(row["id"], row["user_id"], row["name"], data, used_filenames))
    check_players(players)
    return players


def prepare_player(config_id: int | None, user_id: int | None, label: str, data: bytes,
                   used_filenames: set[str]) -> PreparedPlayer:
    """Checks one player file on its own. `used_filenames` collects the file names given out so far."""
    stem = yamlinfo.safe_filename(Path(label).stem, "player")
    filename, counter = f"{stem}.yaml", 2
    while filename.lower() in used_filenames or filename.lower() in ("meta.yaml", "weights.yaml"):
        filename = f"{stem}-{counter}.yaml"
        counter += 1
    used_filenames.add(filename.lower())
    try:
        info = yamlinfo.analyze(yamlinfo.decode_text(data), fallback_name=Path(filename).stem)
    except yamlinfo.YamlError as e:
        raise JobRequestError(f"'{label}' is not valid: {e}") from None
    if info.problems:
        raise JobRequestError(f"'{label}' cannot be generated: {info.problems[0]}")
    return PreparedPlayer(config_id, user_id, label, filename, data, info)


def check_players(players: list[PreparedPlayer]) -> None:
    """Checks the players together: unique names, and every game installed."""
    names: dict[str, str] = {}
    for p in players:
        for name in p.info.player_names:
            if name.casefold() == "archipelago":
                raise JobRequestError(f"'{p.label}': a player cannot be named Archipelago")
            if name.casefold() in names:
                raise JobRequestError(f"Two players are named '{name}' ('{names[name.casefold()]}' and "
                                      f"'{p.label}'). Player names must be unique.")
            names[name.casefold()] = p.label

    if apbridge.STATE.info is None:
        raise JobRequestError(f"Archipelago is unavailable: {apbridge.STATE.error or 'not loaded yet'}", 503)
    missing = apbridge.missing_games({g for p in players for g in p.info.games})
    if missing:
        raise JobRequestError("These games are not installed: " + ", ".join(missing), 409, missing)
    # Generate.py would stop at the same check, after a wait in the queue and with a less helpful message
    absent = apbridge.missing_files({g for p in players for g in p.info.games})
    if absent:
        raise JobRequestError(" ".join(apbridge.missing_file_text(a) for a in absent), 409)


def clean_meta(meta: str | None) -> str | None:
    if meta is None or not meta.strip():
        return None
    try:
        # the player-file loader: it refuses merge keys, which can blow a tiny file up to gigabytes
        doc = list(yaml.load_all(meta, Loader=yamlinfo.UniqueKeyLoader))[-1]
    except yaml.YAMLError as e:
        raise JobRequestError(f"meta.yaml is not valid YAML: {e}") from None
    if not isinstance(doc, dict) or "meta_description" not in doc:
        raise JobRequestError("meta.yaml must contain a 'meta_description' entry, otherwise the generator rejects it")
    return meta


def create_job(user: sqlite3.Row, players: list[PreparedPlayer], *, name: str | None, spoiler: int, race: bool,
               seed: int | None, skip_prog_balancing: bool, plando: list[str], meta: str | None,
               host_after: bool, test: bool = False) -> int:
    """Queues a generation. A test generation only finds out whether the players generate together: its result is
    thrown away instead of becoming a game."""
    if spoiler not in (0, 1, 2, 3):
        raise JobRequestError("Spoiler level must be between 0 and 3")
    if seed is not None and not (0 <= seed < 10 ** 18):
        raise JobRequestError("Seed must be a whole number")
    if any(p not in PLANDO_OPTIONS for p in plando):
        raise JobRequestError("Unknown plando option")
    meta = clean_meta(meta)
    options = {"spoiler": spoiler, "race": race, "seed": seed, "skip_prog_balancing": skip_prog_balancing,
               "plando": plando, "has_meta": meta is not None}
    if test:
        options["test"] = True
    job_id = db.execute(
        "INSERT INTO jobs(name, created_by, status, options, host_after, created_at) VALUES (?,?,?,?,?,?)",
        ((name or "").strip()[:120] or None, user["id"], "queued", db.jdumps(options), int(host_after and not test),
         db.now()))
    directory = job_dir(job_id)
    try:
        (directory / "Players").mkdir(parents=True)
        for p in players:
            (directory / "Players" / p.filename).write_bytes(p.data)
        if meta:
            (directory / "Players" / "meta.yaml").write_text(meta, encoding="utf-8")
        with db.connection() as conn:
            for p in players:
                conn.execute("INSERT INTO job_players(job_id, config_id, user_id, filename, player_names, games, "
                             "sha256) VALUES (?,?,?,?,?,?,?)",
                             (job_id, p.config_id, p.user_id, p.filename, db.jdumps(p.info.player_names),
                              db.jdumps(p.info.games), hashlib.sha256(p.data).hexdigest()))
    except BaseException:
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return job_id


# Generate.py ends a failure with input("Press enter to close."). Our stdin is closed, so Python adds more noise to
# the same line; none of it helps the person reading the error.
_NOISE_PREFIXES = ("Exception ignored in atexit", "EOFError")


def _explain_failure(lines: list[str]) -> str | None:
    """Plain words for failures with a known cause: a base ROM that is missing or the wrong version."""
    for text in lines:
        if "FileNotFoundError:" in text:
            # worlds report the file as a bare path, "...: <path>", or "<path> does not exist, but <setting> is required"
            missing = text.split("FileNotFoundError:", 1)[1].strip().split(" does not exist, but ")[0]
            if ": " in missing and not os.path.isabs(missing):
                missing = missing.rsplit(": ", 1)[-1]
            if missing:
                folder = (apbridge.STATE.where or {}).get("user_path") or str(CONFIG.ap_dir)
                where = apbridge.file_location_text(apbridge.describe_file(Path(folder) / missing))
                return (f"A file needed to generate is missing on the server. If a game in this multiworld needs its "
                        f"base ROM, put {where}, named exactly like that.")
        lowered = text.lower()
        if "rom" in lowered and any(word in lowered for word in ("md5", "hash", "does not match")):
            return ("The base ROM on the server is not the version the game needs. Its setup guide says which region "
                    "and revision to use.")
    return None


# Generate.py lists what is wrong with each player file: "1. File Alice.yaml document #1 (with name: Alice) is
# invalid. Please fix your yaml." and on the following lines the causes, up to an empty line.
_PLAYER_FILE_ERROR = re.compile(r"^\d+\. File (.+?)(?: document #\d+ \(with name: .*\))? is invalid\. Please fix your yaml\.$")


def _player_file_errors(lines: list[str]) -> dict[str, str]:
    """What the generator said is wrong with which player file, by file name."""
    errors: dict[str, list[str]] = {}
    current = None
    for text in lines:
        text = text.replace("Press enter to close.", "").strip()
        match = _PLAYER_FILE_ERROR.match(text)
        if match:
            current = errors.setdefault(os.path.basename(match.group(1)), [text])
        elif current is not None:
            if not text or text.startswith(_NOISE_PREFIXES):
                current = None
            else:
                current.append(text)
    # the causes say what is wrong; only when there are none, the line naming the file has to do
    return {filename: "\n".join(found[1:] or found)[:1500] for filename, found in errors.items()}


def _summarize_failure(lines: list[str]) -> str:
    """Picks the lines of a generator log that explain the failure."""
    cleaned = []
    for text in lines:
        text = text.replace("Press enter to close.", "").rstrip()
        if text.strip() and not text.strip().startswith(_NOISE_PREFIXES):
            cleaned.append(text)
    interesting = [text.strip() for text in cleaned if not text.startswith((" ", "\t", "Traceback"))
                   and any(w in text for w in ("Error", "Exception", "error", "Unsupported", "unbeatable", "Names have"))]
    chosen = interesting[-3:] or [text.strip() for text in cleaned][-3:]
    summary = "\n".join(chosen)[:1500] or "The generator failed without output"
    explanation = _explain_failure(cleaned)
    return f"{explanation}\n{summary}" if explanation else summary


class GenerationManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.current: tuple[int, asyncio.subprocess.Process] | None = None
        self.cancelled: set[int] = set()
        self.logs: dict[int, collections.deque] = {}
        self.seq: dict[int, int] = collections.defaultdict(int)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        db.execute("UPDATE jobs SET status = 'failed', finished_at = ?, "
                   "error = 'The manager was restarted while this job was running' WHERE status = 'running'",
                   (time.time(),))
        for row in db.query("SELECT id FROM jobs WHERE status = 'queued' ORDER BY id"):
            self.queue.put_nowait(row["id"])
        self._prune()
        self.worker = asyncio.create_task(self._work())

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.current:
            self._kill_group(self.current[1], signal.SIGKILL)

    def enqueue(self, job_id: int) -> None:
        """Safe to call from the worker threads that run the API's plain (non-async) routes."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.queue.put_nowait, job_id)

    def queue_position(self, job_id: int) -> int | None:
        waiting = [r["id"] for r in db.query("SELECT id FROM jobs WHERE status = 'queued' ORDER BY id")]
        return waiting.index(job_id) + 1 if job_id in waiting else None

    # ------------------------------------------------------------------------------------------------------------
    def _log(self, job_id: int, line: str) -> None:
        buffer = self.logs.setdefault(job_id, collections.deque(maxlen=3000))
        self.seq[job_id] += 1
        buffer.append((self.seq[job_id], line))
        with contextlib.suppress(OSError):
            with open(job_dir(job_id) / "job.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def log_since(self, job_id: int, after: int = 0) -> dict:
        buffer = self.logs.get(job_id)
        if buffer is None:  # after a restart: serve the file, numbering lines from 1
            try:
                text = (job_dir(job_id) / "job.log").read_text(encoding="utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                text = []
            buffer = collections.deque(enumerate(text[-3000:], start=1), maxlen=3000)
            self.logs[job_id] = buffer
            self.seq[job_id] = len(text[-3000:])
        return {"lines": [{"seq": s, "text": t} for s, t in buffer if s > after],
                "last": self.seq.get(job_id, 0)}

    def _prune(self) -> None:
        finished = db.query("SELECT id FROM jobs WHERE status IN ('succeeded','failed','cancelled') "
                            "ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_FINISHED_JOBS,))
        for row in finished:
            self.remove(row["id"])

    def remove(self, job_id: int) -> None:
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
        self.logs.pop(job_id, None)
        self.seq.pop(job_id, None)

    # ------------------------------------------------------------------------------------------------------------
    async def _work(self) -> None:
        while True:
            job_id = await self.queue.get()
            row = db.query_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
            if not row or row["status"] != "queued":
                continue
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one broken job must not stop the queue
                log.exception("Job %s crashed", job_id)
                self._finish(job_id, "failed", f"Internal error: {e}")

    def _finish(self, job_id: int, status: str, error: str | None = None, game_id: int | None = None) -> None:
        db.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ?, game_id = ? WHERE id = ?",
                   (status, time.time(), error, game_id, job_id))
        job = db.query_one("SELECT options FROM jobs WHERE id = ?", (job_id,))
        # a test is started by an administrator who is looking at its outcome
        if status == "failed" and job and not db.jloads(job["options"], {}).get("test"):
            name, origin = _describe_job(job_id)
            notify.emit("generation", notify.tr("Generation failed: {name}", name=name), f"{origin}\n```\n{error}\n```",
                        url=notify.link(f"/jobs/{job_id}"), color="error")

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    async def _run(self, job_id: int) -> None:
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        opts: dict[str, Any] = db.jloads(job["options"], {})
        directory = job_dir(job_id)
        output = directory / "output"
        output.mkdir(exist_ok=True)
        db.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (time.time(), job_id))

        cmd = apbridge.ap_command("Generate.py", "--player_files_path", str(directory / "Players"),
                                  "--outputpath", str(output), "--spoiler", str(opts.get("spoiler", 1)))
        if opts.get("race"):
            cmd.append("--race")
        if opts.get("seed") is not None:
            cmd += ["--seed", str(opts["seed"])]
        if opts.get("skip_prog_balancing"):
            cmd.append("--skip_prog_balancing")
        cmd += ["--plando", ",".join(opts.get("plando") or []) or "none"]
        if opts.get("has_meta"):
            cmd += ["--meta_file_path", str(directory / "Players" / "meta.yaml")]

        self._log(job_id, "$ " + " ".join(cmd[1:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(CONFIG.ap_dir), env=apbridge.ap_env(), start_new_session=True)
        self.current = (job_id, proc)
        lines: list[str] = []

        async def pump() -> None:
            assert proc.stdout is not None
            while raw := await proc.stdout.readline():
                text = raw.decode(errors="replace").rstrip("\r\n")
                lines.append(text)
                self._log(job_id, text)

        pump_task = asyncio.create_task(pump())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), CONFIG.generation_timeout)
        except TimeoutError:
            timed_out = True
            self._kill_group(proc, signal.SIGKILL)
            await proc.wait()
        finally:
            self.current = None
            # Generate.py spawns worker processes; make sure none survive (this also closes the output pipe)
            self._kill_group(proc, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(pump_task, 10)

        if job_id in self.cancelled:
            self.cancelled.discard(job_id)
            self._finish(job_id, "cancelled", "Cancelled")
            return
        if timed_out:
            self._finish(job_id, "failed", f"Generation took longer than {CONFIG.generation_timeout // 60} minutes")
            return
        if proc.returncode != 0:
            with db.connection() as conn:
                for filename, error in _player_file_errors(lines).items():
                    conn.execute("UPDATE job_players SET error = ? WHERE job_id = ? AND filename = ?",
                                 (error, job_id, filename))
            self._finish(job_id, "failed", _summarize_failure(lines))
            return

        archives = sorted(output.glob("AP_*.zip"), key=lambda p: p.stat().st_mtime)
        if not archives:
            self._finish(job_id, "failed", "The generator finished but produced no output file")
            return
        if opts.get("test"):
            shutil.rmtree(output, ignore_errors=True)
            self._log(job_id, "Test passed: these players generate together. The result was not kept.")
            self._finish(job_id, "succeeded")
            return

        owners = {}
        players = db.query("SELECT * FROM job_players WHERE job_id = ?", (job_id,))
        for p in players:
            for name in db.jloads(p["player_names"], []):
                if p["user_id"] is not None:
                    owners[name.casefold()] = p["user_id"]
        try:
            game_id = await gamestore.import_game(
                archives[-1], job["name"] or "", name=job["name"], created_by=job["created_by"],
                source="generated", job_id=job_id, owners=owners)
        except gamestore.GameImportError as e:
            self._finish(job_id, "failed", f"Generated, but importing the result failed: {e}")
            return
        for p in players:
            source = directory / "Players" / p["filename"]
            if source.is_file():
                tmp = CONFIG.tmp_dir / f"{job_id}-{p['filename']}"
                shutil.copyfile(source, tmp)
                gamestore.add_file(game_id, p["filename"], tmp, kind="yaml")
        shutil.rmtree(output, ignore_errors=True)
        self._log(job_id, f"Imported as game #{game_id}")
        self._finish(job_id, "succeeded", None, game_id)

        started = False
        if job["host_after"]:
            try:
                await ROOMS.start(game_id)
                self._log(job_id, "Server started")
                started = True
            except RoomError as e:
                self._log(job_id, f"Could not start the server: {e}")

        game = db.query_one("SELECT name, port, password FROM games WHERE id = ?", (game_id,))
        name, origin = _describe_job(job_id)
        fields = []
        if started:
            address = notify.game_address(game["port"])
            fields.append((notify.tr("Server"), f"`{address}`" if address else f"Port {game['port']}", True))
            if game["password"]:
                fields.append((notify.tr("Password"), notify.tr("Shown on the game page"), True))
        fields.append((notify.tr("Players"), player_list(game_id)))
        if not started:
            origin += "\n" + notify.tr("An administrator or player can start it from the game page.")
        # its players are told, and if it is already running, also those who want to know when a game goes online
        notify.emit("generation", notify.tr("New game ready: {name}", name=game["name"] or name), origin,
                    url=notify.link(f"/games/{game_id}"), fields=fields, color="success",
                    ping=("game_ready", "game_online") if started else "game_ready", ping_users=slot_owners(game_id),
                    game_id=game_id)

    async def cancel(self, job_id: int) -> bool:
        row = db.query_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
        if not row:
            return False
        if row["status"] == "queued":
            self._finish(job_id, "cancelled", "Cancelled")
            return True
        if row["status"] == "running" and self.current and self.current[0] == job_id:
            self.cancelled.add(job_id)
            proc = self.current[1]
            self._kill_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), CANCEL_GRACE_SECONDS)
            except TimeoutError:
                self._kill_group(proc, signal.SIGKILL)
            return True
        return False


def _describe_job(job_id: int) -> tuple[str, str]:
    """The name of a job and where it came from, for notifications."""
    job = db.query_one("SELECT j.name, u.username FROM jobs j LEFT JOIN users u ON u.id = j.created_by WHERE j.id = ?",
                       (job_id,))
    signup = db.query_one("SELECT name FROM signups WHERE job_id = ?", (job_id,))
    name = (job["name"] if job else None) or notify.tr("Job #{id}", id=job_id)
    if signup:
        return name, notify.tr("From the sign-up round **{name}**.", name=notify.md(signup["name"]))
    if job and job["username"]:
        return name, notify.tr("Generated by {user}.", user=notify.md(job["username"]))
    return name, notify.tr("Generated.")


GENERATION = GenerationManager()
