"""Sign-up rounds: players enter their configs until a deadline, then one multiworld is generated from all entries.

A round is 'open' while it takes entries, 'closed' once the deadline passed or an admin closed it, 'generating' while
its generation job runs and 'done' when the game exists. A failed generation sends it back to 'closed' with the error,
so an admin can fix the entries and try again.

Before that, an admin can test the round: the entries are generated as they are, and the result is thrown away. A failed
test, like a failed generation, marks the entries the generator complained about, so their players see what to fix.
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

from . import apbridge, configstore, db, notify, settings as app_settings, yamlinfo
from .config import CONFIG
from .generation import (GENERATION, PLANDO_OPTIONS, JobRequestError, PreparedPlayer, check_players, create_job,
                         prepare_player)

log = logging.getLogger(__name__)

TICK_SECONDS = 20
REMIND_BEFORE = 24 * 3600
MAX_CONFIGS_PER_USER = 20
DEFAULT_OPTIONS = {"spoiler": 1, "race": False, "skip_prog_balancing": False,
                   "plando": ["bosses", "connections", "texts"], "host_after": True,
                   "server_options": None}  # the game's server settings; None: the defaults for new games

# Closing and generating change a round in several steps. The routes run in worker threads and the deadline check in
# the event loop, so a plain lock keeps them from closing or generating the same round twice.
_LOCK = threading.Lock()


class SignupError(Exception):
    def __init__(self, message: str, status: int = 400, missing_games: list[str] | None = None) -> None:
        super().__init__(message)
        self.message, self.status, self.missing_games = message, status, missing_games or []


def entry_path(signup_id: int, entry_id: int) -> Path:
    return CONFIG.data_dir / "signups" / str(signup_id) / f"{entry_id}.yaml"


def weights_path(signup_id: int) -> Path:
    """A mystery round's file of weights (mystery.py), which every entry is a copy of."""
    return CONFIG.data_dir / "signups" / str(signup_id) / "mystery.yaml"


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
    if result["server_options"] is not None:
        if not isinstance(result["server_options"], dict):
            raise SignupError("The settings for the new game are not valid")
        try:
            result["server_options"] = app_settings.clean_server_options(
                result["server_options"], base=app_settings.get_setting("default_server_options"))
        except app_settings.SettingsError as e:
            raise SignupError(str(e)) from None
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
        if signup["mystery"]:
            raise SignupError("This is a mystery round: join it with your player name", 409)
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


# ----------------------------------------------------------------------------------------------------------------------
# mystery rounds: every entry is the round's file of weights with its player's name (mystery.py)
# ----------------------------------------------------------------------------------------------------------------------
def check_weights(text: str) -> yamlinfo.YamlInfo:
    """A file of weights fit for a mystery round: one player, which the generator can take."""
    try:
        info = yamlinfo.analyze(text, "mystery")
    except yamlinfo.YamlError as e:
        raise SignupError(f"The weights are not valid: {e}") from None
    if len(info.docs) != 1:
        raise SignupError("The weights of a mystery round describe one player, which every entry is a copy of")
    if info.problems:
        raise SignupError(f"The weights cannot be generated: {info.problems[0]}")
    missing = apbridge.missing_games(info.games)
    if missing:
        raise SignupError(f"Not installed on this server: {', '.join(missing)}", 409, missing)
    return info


def weights_games(signup: sqlite3.Row) -> list[str]:
    """The games a mystery round's players may get."""
    try:
        return yamlinfo.analyze(weights_path(signup["id"]).read_text(encoding="utf-8"), "mystery").games
    except (OSError, yamlinfo.YamlError):
        return []


def _mystery_copy(weights: str, player: str) -> tuple[bytes, yamlinfo.YamlInfo]:
    try:
        text = yamlinfo.with_player_name(weights, player)
        return text.encode(), yamlinfo.analyze(text, player)
    except yamlinfo.YamlError as e:
        raise SignupError(str(e)) from None


def set_weights(signup_id: int, text: str) -> None:
    """Sets a mystery round's weights, and makes every entry a copy of them anew. Checked with check_weights first."""
    with _LOCK, db.connection() as conn:
        signup = conn.execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()
        if not signup or signup["status"] not in ("open", "closed"):
            raise SignupError("This round was already generated", 409)
        rows = conn.execute("SELECT id, player_names FROM signup_entries WHERE signup_id = ?", (signup_id,)).fetchall()
        copies = [(row["id"], *_mystery_copy(text, (db.jloads(row["player_names"], []) or ["Player"])[0]))
                  for row in rows]
        for entry_id, data, info in copies:
            conn.execute("UPDATE signup_entries SET games = ?, sha256 = ?, updated_at = ? WHERE id = ?",
                         (db.jdumps(info.games), hashlib.sha256(data).hexdigest(), time.time(), entry_id))
        configstore.write_atomic(weights_path(signup_id), text.encode())
        for entry_id, data, _info in copies:
            configstore.write_atomic(entry_path(signup_id, entry_id), data)


def add_mystery_entry(signup_id: int, user_id: int, player: str, *, by_admin: bool = False) -> dict:
    """Enters a player into a mystery round with nothing but their player name."""
    try:
        weights = weights_path(signup_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SignupError("This is not a mystery round", 409) from None
    data, info = _mystery_copy(weights, player)
    now = time.time()
    with db.connection() as conn:
        signup = conn.execute("SELECT * FROM signups WHERE id = ?", (signup_id,)).fetchone()
        if not _takes_entries(signup, by_admin=by_admin):
            raise SignupError("Sign-ups for this round are closed", 409)
        if not signup["mystery"]:
            raise SignupError("This is not a mystery round", 409)
        _check_entry(signup, info, player, user_id=user_id, ignore_entry=None, conn=conn)
        entry_id = conn.execute(
            "INSERT INTO signup_entries(signup_id, user_id, config_id, name, player_names, games, sha256, created_at, "
            "updated_at) VALUES (?,?,NULL,?,?,?,?,?,?)",
            (signup_id, user_id, "mystery.yaml", db.jdumps(info.player_names), db.jdumps(info.games),
             hashlib.sha256(data).hexdigest(), now, now)).lastrowid
        configstore.write_atomic(entry_path(signup_id, entry_id), data)
    return {"id": entry_id, "warnings": _warnings(info)}


def remove_entry(signup_id: int, entry_id: int) -> None:
    db.execute("DELETE FROM signup_entries WHERE id = ? AND signup_id = ?", (entry_id, signup_id))
    entry_path(signup_id, entry_id).unlink(missing_ok=True)


def _warnings(info: yamlinfo.YamlInfo) -> list[str]:
    missing = apbridge.missing_games(info.games)
    warnings = list(info.warnings)
    if missing:
        warnings.append(f"Not installed on this server yet: {', '.join(missing)}. "
                        "The round can only be generated once an administrator adds it.")
    for item in apbridge.missing_files(info.games):
        warnings.append(f"{item['game']} needs its base ROM on the server to generate, and it is not there yet. "
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
    """Who a generation job is run as, and so who runs the game it makes: the round's creator while they may still
    organize, else whoever asked, else any admin."""
    owner = db.query_one("SELECT * FROM users WHERE id = ? AND role IN ('admin', 'organizer') AND disabled = 0",
                         (signup["created_by"],))
    owner = owner or user or db.query_one("SELECT * FROM users WHERE role = 'admin' AND disabled = 0 ORDER BY id")
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
            next_step = notify.tr("Nobody signed up.")
        elif generate_now:
            next_step = notify.tr("Generating the game now.")
        else:
            next_step = notify.tr("An administrator will generate the game.")
        notify.emit("signup", notify.tr("Sign-ups closed: {name}", name=signup["name"]),
                    notify.tr("{n} entry. {next_step}" if len(rows) == 1 else "{n} entries. {next_step}",
                              n=len(rows), next_step=next_step),
                    url=notify.link(f"/signups/{signup_id}"),
                    fields=[(notify.tr("Players"), _player_lines(rows))] if rows else None, color="info")
    if generate_now:
        try:
            generate(signup_id, user=user)
        except SignupError as e:
            log.warning("Sign-up round %s closed, but generating failed: %s", signup_id, e.message)


def _players(signup: sqlite3.Row, rows: list[sqlite3.Row]) -> list[PreparedPlayer]:
    """The entries as players for the generator, checked like a generation checks them."""
    if not rows:
        raise SignupError("Nobody signed up for this round")
    issues = entry_issues(signup, rows)
    problems = [f"{row['username']}: {issue}" for row in rows for issue in issues[row["id"]]]
    if problems:
        raise SignupError("Some entries break the round's rules: " + "; ".join(problems))
    players, used = [], set()
    for row in rows:
        try:
            data = entry_path(signup["id"], row["id"]).read_bytes()
        except FileNotFoundError:
            raise SignupError(f"The file of {row['username']}'s entry '{row['name']}' is missing", 410) from None
        players.append(prepare_player(row["config_id"], row["user_id"], row["name"], data, used))
    check_players(players)
    return players


def _start_job(signup: sqlite3.Row, rows: list[sqlite3.Row], owner: sqlite3.Row, *, test: bool) -> int:
    opts = clean_options(db.jloads(signup["options"], {}))
    return create_job(owner, _players(signup, rows), name=signup["name"], spoiler=opts["spoiler"], race=opts["race"],
                      seed=None, skip_prog_balancing=opts["skip_prog_balancing"], plando=opts["plando"], meta=None,
                      host_after=opts["host_after"], test=test, server_options=opts["server_options"])


def generate(signup_id: int, *, user: sqlite3.Row | None = None) -> int:
    """Starts generating the round's game from its entries. Returns the job id."""
    with _LOCK:
        signup = get(signup_id)
        if not signup:
            raise SignupError("No such round", 404)
        if signup["status"] != "closed":
            raise SignupError("Close the sign-ups before generating" if signup["status"] == "open"
                              else "This round was already generated", 409)
        try:
            job_id = _start_job(signup, entries(signup_id), _job_owner(signup, user), test=False)
        except (SignupError, JobRequestError) as e:
            db.execute("UPDATE signups SET error = ? WHERE id = ?", (e.message, signup_id))
            if user is None:  # nobody is looking at the page to see the error: this ran at the deadline
                notify.emit("generation", notify.tr("Could not generate {name}", name=signup["name"]), e.message,
                            url=notify.link(f"/signups/{signup_id}"), color="error")
            if isinstance(e, JobRequestError):
                raise SignupError(e.message, e.status, e.missing_games) from None
            raise
        db.execute("UPDATE signups SET status = 'generating', job_id = ?, error = NULL WHERE id = ?",
                   (job_id, signup_id))
    GENERATION.enqueue(job_id)
    return job_id


def test(signup_id: int, *, user: sqlite3.Row) -> int:
    """Generates the current entries without keeping the result, to find problems before the real generation."""
    with _LOCK:
        signup = get(signup_id)
        if not signup:
            raise SignupError("No such round", 404)
        if signup["status"] not in ("open", "closed"):
            raise SignupError("This round was already generated", 409)
        running = db.query_one("SELECT 1 FROM jobs WHERE id = ? AND status IN ('queued', 'running')",
                               (signup["test_job_id"],))
        if running:
            raise SignupError("A test of this round is already running", 409)
        try:
            job_id = _start_job(signup, entries(signup_id), _job_owner(signup, user), test=True)
        except JobRequestError as e:
            raise SignupError(e.message, e.status, e.missing_games) from None
        db.execute("UPDATE signups SET test_job_id = ? WHERE id = ?", (job_id, signup_id))
    GENERATION.enqueue(job_id)
    return job_id


def _same_options(job: sqlite3.Row, signup: sqlite3.Row) -> bool:
    """Whether a job generated with the options the round has now."""
    done, now = db.jloads(job["options"], {}), clean_options(db.jloads(signup["options"], {}))
    return all(done.get(key) == now[key] for key in ("spoiler", "race", "skip_prog_balancing")) and \
        sorted(done.get("plando") or []) == sorted(now["plando"])


def test_result(signup: sqlite3.Row, rows: list[sqlite3.Row]) -> dict | None:
    """The round's latest test, and whether it tested the entries and options the round has now."""
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (signup["test_job_id"],)) if signup["test_job_id"] else None
    if not job:
        return None
    tested = sorted(p["sha256"] for p in db.query("SELECT sha256 FROM job_players WHERE job_id = ?", (job["id"],)))
    return {"job_id": job["id"], "status": job["status"], "error": job["error"], "created_at": job["created_at"],
            "finished_at": job["finished_at"], "queue_position": GENERATION.queue_position(job["id"]),
            "current": tested == sorted(row["sha256"] for row in rows) and _same_options(job, signup)}


def entry_errors(signup: sqlite3.Row, rows: list[sqlite3.Row]) -> dict[int, str]:
    """What the generator said is wrong with an entry, per entry, if the round's latest test or generation failed.

    An entry the player changed since is not the file the generator complained about, so it has no error."""
    if signup["status"] not in ("open", "closed"):
        return {}
    latest = db.query_one("SELECT id, status FROM jobs WHERE id IN (?, ?) AND status IN ('succeeded', 'failed') "
                          "ORDER BY id DESC LIMIT 1", (signup["test_job_id"] or 0, signup["job_id"] or 0))
    if not latest or latest["status"] != "failed":
        return {}
    by_hash = {p["sha256"]: p["error"] for p in db.query(
        "SELECT sha256, error FROM job_players WHERE job_id = ? AND error IS NOT NULL", (latest["id"],))}
    return {row["id"]: by_hash[row["sha256"]] for row in rows if row["sha256"] in by_hash}


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
        text += notify.tr("Enter your YAML until {date} ({when}).", date=notify.timestamp(signup["deadline"], "F"),
                          when=notify.timestamp(signup["deadline"], "R"))
    else:
        text += notify.tr("Enter your YAML until an administrator closes the sign-ups.")
    allowed = db.jloads(signup["allowed_games"], [])
    limit = signup["configs_per_user"]
    fields = [(notify.tr("Games"), notify.md(", ".join(allowed)) if allowed
               else notify.tr("Any game installed on the server"), False),
              (notify.tr("Configs per player"), str(limit), True)]
    notify.emit("signup", notify.tr("Sign-ups reopened: {name}" if reopened else "Sign-ups open: {name}",
                                    name=signup["name"]), text,
                url=notify.link(f"/signups/{signup_id}"), fields=fields, color="info", ping="signup_open")


def _remind(signup: sqlite3.Row) -> None:
    rows = entries(signup["id"])
    deadline = signup["deadline"]
    text = notify.tr("Sign-ups close {when} ({date}).", when=notify.timestamp(deadline, "R"),
                     date=notify.timestamp(deadline, "F"))
    text += " " + notify.tr("{n} entry so far." if len(rows) == 1 else "{n} entries so far.", n=len(rows))
    # a reminder for those who have not entered yet
    missing = {r["id"] for r in db.query("SELECT id FROM users WHERE id NOT IN "
                                         "(SELECT user_id FROM signup_entries WHERE signup_id = ?)", (signup["id"],))}
    notify.emit("signup", notify.tr("Sign-ups for {name} close soon", name=signup["name"]), text,
                url=notify.link(f"/signups/{signup['id']}"),
                fields=[(notify.tr("Players"), _player_lines(rows))] if rows else None, color="warning",
                ping="signup_closing", ping_users=missing)


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
