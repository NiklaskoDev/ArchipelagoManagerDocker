"""Who changed what: every change an administrator makes, and every start and stop of a game, for the administrators.

A middleware (main.py) records each successful state-changing API request of an administrator, and the starts, stops,
restarts, releases and collects of players. The action is named after the route (ACTIONS). What it acted on (a game, a
user, ...) is looked up before the request runs, so a deleted one keeps its name in the log. Routes add what the log
should know about a change with note(): which settings changed, the console command, a new user's name, ...

Passwords never reach the log: routes note which fields changed, not their values, and the console command is masked
like the console itself (rooms.mask_secrets).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from starlette.requests import Request

from . import db

KEEP = 50_000  # entries; older ones go at startup

# (method, route) -> action. Routes not listed are recorded as "METHOD /path" for administrators.
ACTIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/games"): "game.add",
    ("PATCH", "/api/games/{game_id}"): "game.settings",
    ("DELETE", "/api/games/{game_id}"): "game.delete",
    ("POST", "/api/games/{game_id}/test-notification"): "game.test_notification",
    ("POST", "/api/games/{game_id}/start"): "game.start",
    ("POST", "/api/games/{game_id}/stop"): "game.stop",
    ("POST", "/api/games/{game_id}/restart"): "game.restart",
    ("POST", "/api/games/{game_id}/reset-save"): "game.reset",
    ("POST", "/api/games/{game_id}/backups"): "game.backup",
    ("POST", "/api/games/{game_id}/backups/{name}/restore"): "game.restore",
    ("DELETE", "/api/games/{game_id}/backups/{name}"): "game.backup_delete",
    ("POST", "/api/games/{game_id}/command"): "game.command",
    ("POST", "/api/games/{game_id}/slots/{slot}/action"): "slot.action",
    ("POST", "/api/games/{game_id}/slots/{slot}/assign"): "slot.assign",
    ("POST", "/api/games/{game_id}/slots/{slot}/claim"): "slot.claim",
    ("POST", "/api/games/{game_id}/slots/{slot}/unclaim"): "slot.unclaim",
    ("PUT", "/api/games/{game_id}/slots/{slot}/handover"): "slot.handover",
    ("DELETE", "/api/games/{game_id}/slots/{slot}/handover"): "slot.handover_cancel",
    ("POST", "/api/games/{game_id}/slots/{slot}/handover/accept"): "slot.handover_accept",
    ("POST", "/api/games/{game_id}/files"): "game.file_add",
    ("PATCH", "/api/games/{game_id}/files/{file_id}"): "game.file_change",
    ("DELETE", "/api/games/{game_id}/files/{file_id}"): "game.file_delete",
    ("PATCH", "/api/settings"): "settings.change",
    ("POST", "/api/settings/test-notification"): "settings.test_notification",
    ("PUT", "/api/host-yaml"): "settings.host_yaml",
    ("POST", "/api/system/check-updates"): "system.check_updates",
    ("POST", "/api/system/reload-worlds"): "system.reload_worlds",
    ("POST", "/api/system/backup"): "system.backup",
    ("POST", "/api/ap-versions"): "system.version_install",
    ("DELETE", "/api/ap-versions/{version}"): "system.version_remove",
    ("POST", "/api/users"): "user.add",
    ("PATCH", "/api/users/{user_id}"): "user.change",
    ("DELETE", "/api/users/{user_id}"): "user.delete",
    ("POST", "/api/apworlds"): "apworld.add",
    ("POST", "/api/apworlds/{apworld_id}/approve"): "apworld.approve",
    ("DELETE", "/api/apworlds/{apworld_id}"): "apworld.delete",
    ("POST", "/api/apworld-sources"): "source.add",
    ("POST", "/api/apworld-sources/{source_id}/approve"): "source.approve",
    ("POST", "/api/apworld-sources/{source_id}/update"): "source.update",
    ("POST", "/api/apworld-sources/{source_id}/check"): "source.check",
    ("DELETE", "/api/apworld-sources/{source_id}"): "source.delete",
    ("PUT", "/api/configs/{config_id}"): "config.change",
    ("DELETE", "/api/configs/{config_id}"): "config.delete",
    ("PUT", "/api/configs/{config_id}/shared"): "config.share",
    ("POST", "/api/jobs"): "job.start",
    ("POST", "/api/jobs/{job_id}/cancel"): "job.cancel",
    ("DELETE", "/api/jobs/{job_id}"): "job.delete",
    ("POST", "/api/signups"): "signup.add",
    ("PATCH", "/api/signups/{signup_id}"): "signup.change",
    ("DELETE", "/api/signups/{signup_id}"): "signup.delete",
    ("POST", "/api/signups/{signup_id}/close"): "signup.close",
    ("POST", "/api/signups/{signup_id}/reopen"): "signup.reopen",
    ("POST", "/api/signups/{signup_id}/generate"): "signup.generate",
    ("POST", "/api/signups/{signup_id}/test"): "signup.test",
    ("DELETE", "/api/signups/{signup_id}/entries/{entry_id}"): "signup.entry_delete",
    ("POST", "/api/roms"): "rom.upload",
    ("DELETE", "/api/roms"): "rom.delete",
}
# What a player also does for themselves, and what changes nothing: not worth a line, even for an administrator.
# A wake gets its line from the route, when it really started the server.
SKIP = {("POST", "/api/configs"), ("POST", "/api/configs/text"), ("POST", "/api/configs/{config_id}/copy"),
        ("POST", "/api/games/{game_id}/wake"), ("POST", "/api/games/{game_id}/visit"),
        ("PUT", "/api/games/{game_id}/slots/{slot}/status"),
        ("POST", "/api/configs/form/read"), ("POST", "/api/configs/form/write"),
        ("POST", "/api/signups/{signup_id}/entries"), ("POST", "/api/signups/{signup_id}/entries/{entry_id}/refresh")}
# Recorded for everyone, not only administrators
FOR_EVERYONE = {"game.start", "game.stop", "game.restart", "slot.action", "slot.handover_accept"}

# What a request acts on, from its path, and how to find its name
_TARGETS = [
    (re.compile(r"^/api/games/(\d+)(?:/|$)"), "game", "SELECT name FROM games WHERE id = ?"),
    (re.compile(r"^/api/users/(\d+)$"), "user", "SELECT username FROM users WHERE id = ?"),
    (re.compile(r"^/api/signups/(\d+)(?:/|$)"), "signup", "SELECT name FROM signups WHERE id = ?"),
    (re.compile(r"^/api/jobs/(\d+)(?:/|$)"), "job", "SELECT name FROM jobs WHERE id = ?"),
    (re.compile(r"^/api/apworlds/(\d+)(?:/|$)"), "apworld", "SELECT game FROM apworlds WHERE id = ?"),
    (re.compile(r"^/api/apworld-sources/(\d+)(?:/|$)"), "source", "SELECT repo FROM apworld_sources WHERE id = ?"),
    (re.compile(r"^/api/configs/(\d+)(?:/|$)"), "config", "SELECT name FROM configs WHERE id = ?"),
]
_ID_PARAMS = {"game_id", "user_id", "signup_id", "job_id", "apworld_id", "source_id", "config_id"}


def watched(request: Request) -> bool:
    """Whether a request may need a line, before anything is looked up: the text client's polling is the most common
    request, and signing in or out is not an administrator's change."""
    path = request.url.path
    return (request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/")
            and not path.startswith("/api/auth/") and "/client" not in path)


def target(path: str) -> tuple[str, int, str | None] | None:
    """What a request acts on, with its name as it is before the request runs."""
    for pattern, kind, query in _TARGETS:
        if match := pattern.match(path):
            row = db.query_one(query, (int(match[1]),))
            return kind, int(match[1]), (row[0] if row else None)
    return None


def note(request: Request, **details: Any) -> None:
    """Something the log should know about this request. `target=(kind, id, name)` names what it created."""
    request.scope.setdefault("audit", {}).update(details)


def finish(request: Request, user: sqlite3.Row, before: tuple | None) -> None:
    """After a successful request: its line, if it gets one."""
    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    key = (request.method, template)
    if key in SKIP:
        return
    action = ACTIONS.get(key, f"{request.method} {template}")
    if user["role"] != "admin" and action not in FOR_EVERYONE:
        return
    details = dict(request.scope.get("audit") or {})
    what = details.pop("target", None) or before
    for name, value in (request.scope.get("path_params") or {}).items():
        if name not in _ID_PARAMS:
            details.setdefault(name, value)
    record(user, action, what, details or None)


def record(user: sqlite3.Row, action: str, what: tuple | None = None, details: dict | None = None) -> None:
    kind, ident, name = what if what else (None, None, None)
    db.execute("INSERT INTO audit_log(at, user_id, username, action, target_kind, target_id, target_name, details) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (time.time(), user["id"], user["username"], action, kind, ident, name,
                json.dumps(details, ensure_ascii=False) if details else None))


def entries(*, before: int | None = None, user_id: int | None = None, action: str | None = None,
            limit: int = 100) -> list[dict]:
    where, args = [], []
    if before:
        where.append("id < ?")
        args.append(before)
    if user_id:
        where.append("user_id = ?")
        args.append(user_id)
    if action:
        where.append("(action = ? OR action LIKE ?)")
        args += [action, f"{action}.%"]
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    return [{"id": r["id"], "at": r["at"], "user_id": r["user_id"], "username": r["username"], "action": r["action"],
             "target": {"kind": r["target_kind"], "id": r["target_id"], "name": r["target_name"]}
             if r["target_kind"] else None,
             "details": json.loads(r["details"]) if r["details"] else None}
            for r in db.query(sql, (*args, limit))]


def prune() -> None:
    db.execute("DELETE FROM audit_log WHERE id <= (SELECT MAX(id) FROM audit_log) - ?", (KEEP,))
