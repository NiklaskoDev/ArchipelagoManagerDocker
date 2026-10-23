"""SQLite access. One short-lived connection per operation keeps this safe to use from threads and the event loop."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any
from collections.abc import Iterable, Iterator

from .config import CONFIG

SCHEMA_VERSION = 9

# Added in schema version 3
_SIGNUPS = """
-- A sign-up round: players enter configs until the deadline, then one multiworld is generated from all of them.
CREATE TABLE signups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT,
    created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at       REAL NOT NULL,
    deadline         REAL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'generating', 'done')),
    configs_per_user INTEGER NOT NULL DEFAULT 1,
    allowed_games    TEXT NOT NULL DEFAULT '[]',
    auto_generate    INTEGER NOT NULL DEFAULT 1,
    options          TEXT NOT NULL DEFAULT '{}',
    closed_at        REAL,
    reminded         INTEGER NOT NULL DEFAULT 0,
    job_id           INTEGER,
    game_id          INTEGER,
    error            TEXT
);

-- An entry is a copy of the player's config taken when they entered, so what gets generated is what they submitted.
-- config_id remembers which library config it came from, to offer an update when that one changes.
CREATE TABLE signup_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signup_id    INTEGER NOT NULL REFERENCES signups(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_id    INTEGER REFERENCES configs(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    player_names TEXT NOT NULL DEFAULT '[]',
    games        TEXT NOT NULL DEFAULT '[]',
    sha256       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX signup_entries_signup ON signup_entries(signup_id);
"""

# Added in schema version 4
_SOURCES = """
-- GitHub repositories whose releases provide a custom game. 'pending' ones were proposed by a player and are not used
-- until an administrator approves them.
CREATE TABLE apworld_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    asset         TEXT,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
    note          TEXT,
    added_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    added_at      REAL NOT NULL,
    approved_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at   REAL,
    game          TEXT,
    installed_tag TEXT,
    apworld_id    INTEGER,
    latest_tag    TEXT,
    latest_url    TEXT,
    checked_at    REAL,
    announced_tag TEXT,
    error         TEXT
);
"""

# Added in schema version 7
_PROGRESS = """
-- How far each player got over time: a point whenever the number of checked locations of a slot changed.
CREATE TABLE game_progress (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    slot    INTEGER NOT NULL,
    at      REAL NOT NULL,
    checked INTEGER NOT NULL
);
CREATE INDEX game_progress_game ON game_progress(game_id, slot, at);
"""

SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL,
    discord_id    TEXT,
    discord_name  TEXT,
    discord_pings TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX users_discord ON users(discord_id) WHERE discord_id IS NOT NULL;

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Player yaml files. The content lives on disk (configs/<user_id>/<id>.yaml), everything queryable is cached here.
CREATE TABLE configs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    player_names   TEXT NOT NULL DEFAULT '[]',
    games          TEXT NOT NULL DEFAULT '[]',
    description    TEXT,
    sha256         TEXT NOT NULL,
    size           INTEGER NOT NULL,
    source_game_id INTEGER,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    shared_at      REAL
);
CREATE INDEX configs_user ON configs(user_id);

-- Custom worlds. Only one 'installed' row per game; 'pending' rows wait for an admin to approve them.
CREATE TABLE apworlds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game          TEXT NOT NULL,
    module        TEXT NOT NULL,
    original_name TEXT NOT NULL,
    version       TEXT,
    status        TEXT NOT NULL CHECK (status IN ('installed', 'pending')),
    size          INTEGER NOT NULL,
    manifest      TEXT NOT NULL DEFAULT '{}',
    uploaded_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at   REAL NOT NULL,
    load_error    TEXT
);

-- A hosted (or hostable) multiworld: one multidata file, one server process.
CREATE TABLE games (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    seed_name         TEXT,
    source            TEXT NOT NULL DEFAULT 'upload',
    created_at        REAL NOT NULL,
    created_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    job_id            INTEGER,
    generator_version TEXT,
    min_server_version TEXT,
    race_mode         INTEGER NOT NULL DEFAULT 0,
    games             TEXT NOT NULL DEFAULT '[]',
    port              INTEGER,
    password          TEXT,
    server_password   TEXT,
    options           TEXT NOT NULL DEFAULT '{}',
    desired_state     TEXT NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running', 'stopped')),
    visibility        TEXT NOT NULL DEFAULT 'participants' CHECK (visibility IN ('participants', 'all')),
    claimable         INTEGER NOT NULL DEFAULT 0,
    single_claim      INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    play_started_at   REAL,
    finished_at       REAL,
    share_spoiler_when_done INTEGER NOT NULL DEFAULT 0,
    discord_webhook_url TEXT
);
CREATE UNIQUE INDEX games_port ON games(port) WHERE port IS NOT NULL;

CREATE TABLE game_slots (
    game_id   INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    slot      INTEGER NOT NULL,
    name      TEXT NOT NULL,
    game      TEXT NOT NULL,
    type      INTEGER NOT NULL DEFAULT 1,
    locations INTEGER NOT NULL DEFAULT 0,
    user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    goal_at   REAL,
    PRIMARY KEY (game_id, slot)
);

CREATE TABLE game_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    slot         INTEGER,
    filename     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('patch', 'spoiler', 'yaml', 'other')),
    public       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (game_id, filename)
);

CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    options     TEXT NOT NULL DEFAULT '{}',
    host_after  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    game_id     INTEGER,
    error       TEXT
);

CREATE TABLE job_players (
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    config_id    INTEGER,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    filename     TEXT NOT NULL,
    player_names TEXT NOT NULL DEFAULT '[]',
    games        TEXT NOT NULL DEFAULT '[]',
    sha256       TEXT,
    error        TEXT
);
CREATE INDEX job_players_job ON job_players(job_id);
""" + _SIGNUPS + _SOURCES + "ALTER TABLE apworld_sources ADD COLUMN latest_asset TEXT; " + \
    "ALTER TABLE signups ADD COLUMN test_job_id INTEGER;" + _PROGRESS


# Upgrades for databases created by older versions: MIGRATIONS[n] turns schema n - 1 into schema n. New databases get
# the current SCHEMA directly.
MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE games ADD COLUMN single_claim INTEGER NOT NULL DEFAULT 0;",
    3: _SIGNUPS,
    4: _SOURCES,
    # Discord accounts linked to users (an empty password_hash means the account signs in with Discord only)
    5: "ALTER TABLE users ADD COLUMN discord_id TEXT; ALTER TABLE users ADD COLUMN discord_name TEXT; "
       "CREATE UNIQUE INDEX users_discord ON users(discord_id) WHERE discord_id IS NOT NULL; "
       # which exact file of the newest release an administrator was shown (see sources.fingerprint)
       "ALTER TABLE apworld_sources ADD COLUMN latest_asset TEXT;",
    # the hash of each player file and what the generator said was wrong with it; a sign-up round's test generation
    6: "ALTER TABLE job_players ADD COLUMN sha256 TEXT; ALTER TABLE job_players ADD COLUMN error TEXT; "
       "ALTER TABLE signups ADD COLUMN test_job_id INTEGER;",
    # progress over time: when play began, when each player reached their goal, when the game was finished
    7: _PROGRESS + "ALTER TABLE game_slots ADD COLUMN goal_at REAL; ALTER TABLE games ADD COLUMN play_started_at REAL; "
       "ALTER TABLE games ADD COLUMN finished_at REAL; "
       "ALTER TABLE games ADD COLUMN share_spoiler_when_done INTEGER NOT NULL DEFAULT 0;",
    # what a user with a linked Discord account wants to be mentioned for (notify.PING_KINDS)
    8: "ALTER TABLE users ADD COLUMN discord_pings TEXT NOT NULL DEFAULT '[]';",
    # a game's own Discord channel; configs their owner shares with the other players (since when)
    9: "ALTER TABLE games ADD COLUMN discord_webhook_url TEXT; ALTER TABLE configs ADD COLUMN shared_at REAL;",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    CONFIG.ensure_dirs()
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        elif version < SCHEMA_VERSION:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                conn.executescript("BEGIN;" + MIGRATIONS[step] + f"PRAGMA user_version = {step}; COMMIT;")
        elif version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{version} is newer than this version of the manager "
                               f"(v{SCHEMA_VERSION}). Refusing to downgrade.")
    finally:
        conn.close()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """A connection wrapped in a single transaction: commits on success, rolls back on error."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Runs one statement and returns lastrowid."""
    conn = _connect()
    try:
        return conn.execute(sql, tuple(params)).lastrowid or 0
    finally:
        conn.close()


def now() -> float:
    return time.time()


def jloads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def jdumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
