"""Sign-up rounds: players enter their configs until a deadline, then one multiworld is generated from all entries.

A round is 'open' while it takes entries, 'closed' once the deadline passed or an admin closed it, 'generating' while
its generation job runs and 'done' when the game exists. A failed generation sends it back to 'closed' with the error,
so an admin can fix the entries and try again.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import apbridge, configstore, db, notify, yamlinfo
from .config import CONFIG
from .generation import GENERATION, PLANDO_OPTIONS, JobRequestError, check_players, create_job, prepare_player

log = logging.getLogger(__name__)

TICK_SECONDS = 20
REMIND_BEFORE = 24 * 3600
MAX_CONFIGS_PER_USER = 20
DEFAULT_OPTIONS = {"spoiler": 1, "race": False, "skip_prog_balancing": False,
                   "plando": ["bosses", "connections", "texts"], "host_after": True}

# Closing and generating change a round in several steps. The routes run in worker threads and the deadline check in
# the event loop, so a plain lock keeps them from closing or generating the same round twice.
_LOCK = threading.Lock()


class SignupError(Exception):
    def __init__(self, message: str, status: int = 400, missing_games: list[str] | None = None) -> None:
        super().__init__(message)
        self.message, self.status, self.missing_games = message, status, missing_games or []


def entry_path(signup_id: int, entry_id: int) -> Path:
    return CONFIG.data_dir / "signups" / str(signup_id) / f"{entry_id}.yaml"


def get(signup_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM signups WHERE id = ?", (signup_id,))


def entries(signup_id: int) -> list[sqlite3.Row]:
    return db.query("SELECT e.*, u.username, c.sha256 AS library_sha256 FROM signup_entries e "
                    "JOIN users u ON u.id = e.user_id LEFT JOIN configs c ON c.id = e.config_id "
                    "WHERE e.signup_id = ? ORDER BY e.created_at, e.id", (signup_id,))


def is_open(row: sqlite3.Row, now: float | None = None) -> bool:
    return row["status"] == "open" and (row["deadline"] is None or row["deadline"] > (now or time.time()))


def clean_options(options: dict | None) -> dict:
    result = dict(DEFAULT_OPTIONS)
    for key, value in (options or {}).items():
        if key not in DEFAULT_OPTIONS:
            raise SignupError(f"Unknown option: {key}")
        result[key] = value
    if isinstance(result["spoiler"], bool) or result["spoiler"] not in (0, 1, 2, 3):
        raise SignupError("Spoiler level must be between 0 and 3")
    for key in ("race", "skip_prog_balancing", "host_after"):
        if not isinstance(result[key], bool):
            raise SignupError(f"{key} must be true or false")
    if not isinstance(result["plando"], list) or any(p not in PLANDO_OPTIONS for p in result["plando"]):
        raise SignupError("Unknown plando option")
    return result


def clean_allowed_games(games: list[str] | None) -> list[str]:
    result = sorted({g.strip() for g in games or [] if isinstance(g, str) and g.strip()}, key=str.lower)
    if len(result) > 200:
        raise SignupError("Too many games")
    return result


# ----------------------------------------------------------------------------------------------------------------------
# entries
# ----------------------------------------------------------------------------------------------------------------------
def _check_entry(signup: sqlite3.Row, info: yamlinfo.YamlInfo, label: str, *, user_id: int,
                 ignore_entry: int | None, conn: sqlite3.Connection) -> None:
    """The rules of the round, checked against the other entries inside the caller's transaction."""
    if info.problems:
        raise SignupError(f"'{label}' cannot be generated: {info.problems[0]}")
    allowed = db.jloads(signup["allowed_games"], [])
    if allowed:
        other = [g for g in info.games if g not in allowed]
        if other:
            raise SignupError(f"This round only allows {', '.join(allowed)}. '{label}' uses {', '.join(other)}.")
    own = conn.execute("SELECT COUNT(*) FROM signup_entries WHERE signup_id = ? AND user_id = ? AND id != ?",
                       (signup["id"], user_id, ignore_entry or 0)).fetchone()[0]
    if own >= signup["configs_per_user"]:
        limit = signup["configs_per_user"]
        raise SignupError(f"Each player can enter {limit} config{'s' if limit != 1 else ''} in this round. "
                          "Withdraw one first.", 409)
    taken: dict[str, str] = {}
    for row in conn.execute("SELECT e.player_names, u.username FROM signup_entries e JOIN users u ON u.id = e.user_id "
                            "WHERE e.signup_id = ? AND e.id != ?", (signup["id"], ignore_entry or 0)):
        for name in db.jloads(row["player_names"], []):
            taken[name.casefold()] = row["username"]
    for name in info.player_names:
        if name.casefold() == "archipelago":
            raise SignupError("A player cannot be named Archipelago")
        if name.casefold() in taken:
            raise SignupError(f"The player name '{name}' is already taken in this round (by {taken[name.casefold()]}). "
                              "Change the name in your config and try again.", 409)


def _read_library_config(config: sqlite3.Row) -> tuple[bytes, yamlinfo.YamlInfo]:
    try:
        data = configstore.config_path(config["user_id"], config["id"]).read_bytes()
    except FileNotFoundError:
        raise SignupError(f"The file for '{config['name']}' is missing from disk", 410) from None
    try:
        _text, info = configstore.analyze_bytes(data, config["name"])
    except yamlinfo.YamlError as e:
        raise SignupError(f"'{config['name']}' is not valid: {e}") from None
    return data, info


def _takes_entries(signup: sqlite3.Row | None, *, by_admin: bool) -> bool:
    """Players can enter while the round is open; an admin can still fix entries until it is generated."""
    return bool(signup) and (is_open(signup) or (by_admin and signup["status"] in ("open", "closed")))


def add_entry(signup_id: int, config: sqlite3.Row, *, by_admin: bool = False) -> dict:
    """Enters a library config for its owner. Returns the new entry's id and any warnings."""
    data, info = _read_library_config(config)
    now = time.time()
    with db.connection() as conn:
        signup = conn.execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()
        if not _takes_entries(signup, by_admin=by_admin):
            raise SignupError("Sign-ups for this round are closed", 409)
        if conn.execute("SELECT 1 FROM signup_entries WHERE signup_id = ? AND config_id = ?",
                        (signup_id, config["id"])).fetchone():
            raise SignupError(f"'{config['name']}' is already entered in this round", 409)
        _check_entry(signup, info, config["name"], user_id=config["user_id"], ignore_entry=None, conn=conn)
        entry_id = conn.execute(
            "INSERT INTO signup_entries(signup_id, user_id, config_id, name, player_names, games, sha256, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signup_id, config["user_id"], config["id"], config["name"], db.jdumps(info.player_names),
             db.jdumps(info.games), hashlib.sha256(data).hexdigest(), now, now)).lastrowid
        configstore.write_atomic(entry_path(signup_id, entry_id), data)
    return {"id": entry_id, "warnings": _warnings(info)}


def refresh_entry(signup_id: int, entry: sqlite3.Row, *, by_admin: bool = False) -> dict:
    """Replaces an entry with the current version of the library config it came from."""
    config = db.query_one("SELECT * FROM configs WHERE id = ?", (entry["config_id"],)) if entry["config_id"] else None
    if not config:
        raise SignupError("The config this entry came from no longer exists. Withdraw it and enter another one.", 410)
    data, info = _read_library_config(config)
    with db.connection() as conn:
        signup = conn.execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()
        if not _takes_entries(signup, by_admin=by_admin):
            raise SignupError("Sign-ups for this round are closed", 409)
        _check_entry(signup, info, config["name"], user_id=entry["user_id"], ignore_entry=entry["id"], conn=conn)
        conn.execute("UPDATE signup_entries SET name = ?, player_names = ?, games = ?, sha256 = ?, updated_at = ? "
                     "WHERE id = ?", (config["name"], db.jdumps(info.player_names), db.jdumps(info.games),
                                      hashlib.sha256(data).hexdigest(), time.time(), entry["id"]))
        configstore.write_atomic(entry_path(signup_id, entry["id"]), data)
    return {"id": entry["id"], "warnings": _warnings(info)}


def remove_entry(signup_id: int, entry_id: int) -> None:
    db.execute("DELETE FROM signup_entries WHERE id = ? AND signup_id = ?", (entry_id, signup_id))
    entry_path(signup_id, entry_id).unlink(missing_ok=True)


def _warnings(info: yamlinfo.YamlInfo) -> list[str]:
    missing = apbridge.missing_games(info.games)
    warnings = list(info.warnings)
    if missing:
        warnings.append(f"Not installed on this server yet: {', '.join(missing)}. "
                        "The round can only be generated once an administrator adds it.")
    return warnings


def entry_issues(signup: sqlite3.Row, rows: list[sqlite3.Row]) -> dict[int, list[str]]:
    """Problems that would stop generation, per entry: games the round does not allow and clashing player names.
    (Entries are checked when made, but the round's rules can change afterwards.)"""
    allowed = db.jloads(signup["allowed_games"], [])
    owners: dict[str, list[int]] = {}
    for row in rows:
        for name in db.jloads(row["player_names"], []):
            owners.setdefault(name.casefold(), []).append(row["id"])
    issues: dict[int, list[str]] = {}
    for row in rows:
        found = []
        other = [g for g in db.jloads(row["games"], []) if allowed and g not in allowed]
        if other:
            found.append(f"Not allowed in this round: {', '.join(other)}")
        for name in db.jloads(row["player_names"], []):
            if len(owners[name.casefold()]) > 1:
                found.append(f"Another entry also uses the name {name}")
        issues[row["id"]] = found
    return issues


# ----------------------------------------------------------------------------------------------------------------------
# closing and generating
# ----------------------------------------------------------------------------------------------------------------------
def _player_lines(rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in rows:
        names = ", ".join(db.jloads(row["player_names"], [])) or row["name"]
        games = ", ".join(db.jloads(row["games"], []))
        lines.append(f"{notify.md(names)} ({notify.md(games)}) · {notify.md(row['username'])}")
    return "\n".join(lines)


def _job_owner(signup: sqlite3.Row, user: sqlite3.Row | None) -> sqlite3.Row:
    """Who a generation job is run as: the admin who asked, else the round's creator, else any admin."""
    if user is not None:
        return user
    owner = db.query_one("SELECT * FROM users WHERE id = ? AND role = 'admin' AND disabled = 0", (signup["created_by"],))
    owner = owner or db.query_one("SELECT * FROM users WHERE role = 'admin' AND disabled = 0 ORDER BY id")
    if owner is None:
        raise SignupError("There is no administrator account to run the generation as", 409)
    return owner


def close(signup_id: int, *, user: sqlite3.Row | None = None) -> None:
    """Stops taking entries and, if the round says so, starts generating."""
    with _LOCK:
        signup = get(signup_id)
        if not signup or signup["status"] != "open":
            raise SignupError("This round is not open", 409)
        db.execute("UPDATE signups SET status = 'closed', closed_at = ?, error = NULL WHERE id = ?",
                   (time.time(), signup_id))
        rows = entries(signup_id)
        generate_now = bool(signup["auto_generate"] and rows)
        if not rows:
            next_step = "Nobody signed up."
        elif generate_now:
            next_step = "Generating the game now."
        else:
            next_step = "An administrator will generate the game."
        notify.emit("signup", f"Sign-ups closed: {signup['name']}",
                    f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'}. {next_step}",
                    url=notify.link(f"/signups/{signup_id}"),
                    fields=[("Players", _player_lines(rows))] if rows else None, color="info")
    if generate_now:
        try:
            generate(signup_id, user=user)
        except SignupError as e:
            log.warning("Sign-up round %s closed, but generating failed: %s", signup_id, e.message)


def generate(signup_id: int, *, user: sqlite3.Row | None = None) -> int:
    """Starts generating the round's game from its entries. Returns the job id."""
    with _LOCK:
        signup = get(signup_id)
        if not signup:
            raise SignupError("No such round", 404)
        if signup["status"] != "closed":
            raise SignupError("Close the sign-ups before generating" if signup["status"] == "open"
                              else "This round was already generated", 409)
        rows = entries(signup_id)
        try:
            if not rows:
                raise SignupError("Nobody signed up for this round")
            issues = entry_issues(signup, rows)
            problems = [f"{row['username']}: {issue}" for row in rows for issue in issues[row["id"]]]
            if problems:
                raise SignupError("Some entries break the round's rules: " + "; ".join(problems))
            players, used = [], set()
            for row in rows:
                try:
                    data = entry_path(signup_id, row["id"]).read_bytes()
                except FileNotFoundError:
                    raise SignupError(f"The file of {row['username']}'s entry '{row['name']}' is missing", 410) from None
                players.append(prepare_player(row["config_id"], row["user_id"], row["name"], data, used))
            check_players(players)
            opts = clean_options(db.jloads(signup["options"], {}))
            job_id = create_job(_job_owner(signup, user), players, name=signup["name"], spoiler=opts["spoiler"],
                                race=opts["race"], seed=None, skip_prog_balancing=opts["skip_prog_balancing"],
                                plando=opts["plando"], meta=None, host_after=opts["host_after"])
        except (SignupError, JobRequestError) as e:
            db.execute("UPDATE signups SET error = ? WHERE id = ?", (e.message, signup_id))
            if user is None:  # nobody is looking at the page to see the error: this ran at the deadline
                notify.emit("generation", f"Could not generate {signup['name']}", e.message,
                            url=notify.link(f"/signups/{signup_id}"), color="error")
            if isinstance(e, JobRequestError):
                raise SignupError(e.message, e.status, e.missing_games) from None
            raise
        db.execute("UPDATE signups SET status = 'generating', job_id = ?, error = NULL WHERE id = ?",
                   (job_id, signup_id))
    GENERATION.enqueue(job_id)
    return job_id


def reopen(signup_id: int, deadline: float | None) -> None:
    with _LOCK:
        signup = get(signup_id)
        if not signup or signup["status"] != "closed":
            raise SignupError("Only a closed round that was not generated yet can be reopened", 409)
        if deadline is not None and deadline <= time.time():
            raise SignupError("The new deadline is in the past")
        db.execute("UPDATE signups SET status = 'open', deadline = ?, closed_at = NULL, reminded = 0, error = NULL "
                   "WHERE id = ?", (deadline, signup_id))
    announce_open(signup_id, reopened=True)


def sync(signup: sqlite3.Row) -> sqlite3.Row:
    """Takes over the outcome of the round's generation job once it has one."""
    if signup["status"] != "generating":
        return signup
    job = db.query_one("SELECT status, error, game_id FROM jobs WHERE id = ?", (signup["job_id"],))
    if job and job["status"] == "succeeded":
        db.execute("UPDATE signups SET status = 'done', game_id = ? WHERE id = ? AND status = 'generating'",
                   (job["game_id"], signup["id"]))
    elif not job or job["status"] in ("failed", "cancelled"):
        error = (job["error"] if job else None) or "The generation job was cancelled or deleted"
        db.execute("UPDATE signups SET status = 'closed', error = ? WHERE id = ? AND status = 'generating'",
                   (f"Generation failed: {error}", signup["id"]))
    return get(signup["id"])


def delete(signup_id: int) -> None:
    with _LOCK:
        signup = get(signup_id)
        if signup and signup["status"] == "generating":
            raise SignupError("Wait until the generation has finished", 409)
        db.execute("DELETE FROM signups WHERE id = ?", (signup_id,))
    shutil.rmtree(CONFIG.data_dir / "signups" / str(signup_id), ignore_errors=True)


# ----------------------------------------------------------------------------------------------------------------------
# announcements and the deadline clock
# ----------------------------------------------------------------------------------------------------------------------
def announce_open(signup_id: int, *, reopened: bool = False) -> None:
    signup = get(signup_id)
    if not signup:
        return
    text = signup["description"] + "\n\n" if signup["description"] else ""
    if signup["deadline"]:
        text += (f"Enter your YAML until {notify.timestamp(signup['deadline'], 'F')} "
                 f"({notify.timestamp(signup['deadline'], 'R')}).")
    else:
        text += "Enter your YAML until an administrator closes the sign-ups."
    allowed = db.jloads(signup["allowed_games"], [])
    limit = signup["configs_per_user"]
    fields = [("Games", notify.md(", ".join(allowed)) if allowed else "Any game installed on the server", False),
              ("Configs per player", str(limit), True)]
    notify.emit("signup", f"Sign-ups {'reopened' if reopened else 'open'}: {signup['name']}", text,
                url=notify.link(f"/signups/{signup_id}"), fields=fields, color="info")


def _remind(signup: sqlite3.Row) -> None:
    rows = entries(signup["id"])
    deadline = signup["deadline"]
    notify.emit("signup", f"Sign-ups for {signup['name']} close soon",
                f"Sign-ups close {notify.timestamp(deadline, 'R')} ({notify.timestamp(deadline, 'F')}). "
                f"{len(rows)} {'entry' if len(rows) == 1 else 'entries'} so far.",
                url=notify.link(f"/signups/{signup['id']}"),
                fields=[("Players", _player_lines(rows))] if rows else None, color="warning")


def tick(now: float | None = None) -> None:
    now = now or time.time()
    for signup in db.query("SELECT * FROM signups WHERE status = 'open' AND deadline IS NOT NULL"):
        if signup["deadline"] <= now:
            try:
                close(signup["id"])
            except SignupError:
                pass  # closed by an admin in the meantime
        elif (not signup["reminded"] and signup["deadline"] - now <= REMIND_BEFORE
              and signup["deadline"] - signup["created_at"] > REMIND_BEFORE * 1.5):
            # Only rounds that were open well over a day get a reminder; for shorter ones the announcement suffices.
            db.execute("UPDATE signups SET reminded = 1 WHERE id = ?", (signup["id"],))
            _remind(signup)
    for signup in db.query("SELECT * FROM signups WHERE status = 'generating'"):
        sync(signup)


async def run_periodically() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - keep the clock running
            log.exception("Checking sign-up deadlines failed")
        await asyncio.sleep(TICK_SECONDS)
