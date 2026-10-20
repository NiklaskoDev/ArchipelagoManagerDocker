"""SQLite access. One short-lived connection per operation keeps this safe to use from threads and the event loop."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any
from collections.abc import Iterable, Iterator

from .config import CONFIG

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL
);

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
    updated_at     REAL NOT NULL
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
    notes             TEXT
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
    games        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX job_players_job ON job_players(job_id);
"""


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
