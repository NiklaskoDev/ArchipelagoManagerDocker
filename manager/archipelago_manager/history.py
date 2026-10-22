"""How far each player got over time, and the archive of finished games.

Every look at a game's save (rooms.py, about once a minute while it runs) is recorded here: a point whenever a slot's
number of checked locations changed, the time a slot was first seen at its goal, the time the first location of the
game was checked, and the time everyone had reached their goal, which makes the game finished. An administrator can
also finish a game by hand, for one that will not be played to the end.

The save only says how things stand, not when they happened, so times are when the manager saw them (at most a minute
late), except for checks: the save records when each slot last checked a location. Progress made before the manager
kept a history (an older version, or a seed uploaded half played) is noted as it stood at the first look.
"""
from __future__ import annotations

import time
from typing import Any

from . import db

STATUS_GOAL = 30  # ClientStatus.CLIENT_GOAL
MAX_POINTS = 400  # per slot in the chart; more are thinned out evenly


def record(game_id: int, state: dict, now: float | None = None) -> bool:
    """Notes what changed since the last look at the save. Returns True when this look finished the game."""
    now = now or time.time()
    players = db.query("SELECT slot, goal_at FROM game_slots WHERE game_id = ? AND type = 1 ORDER BY slot", (game_id,))
    game = db.query_one("SELECT play_started_at, finished_at FROM games WHERE id = ?", (game_id,))
    if not players or not game:
        return False
    # SQLite takes the other columns of a MAX() row from that row: the latest point of each slot
    last = {r["slot"]: r for r in db.query("SELECT slot, MAX(at) AS at, checked FROM game_progress WHERE game_id = ? "
                                           "GROUP BY slot", (game_id,))}
    first_look = not last
    checked_before = sum(r["checked"] for r in last.values())
    started = game["play_started_at"]
    new_goal = False
    with db.connection() as conn:
        for player in players:
            slot = player["slot"]
            info = state["slots"].get(slot, {})
            checked = int(info.get("checked") or 0)
            previous = last.get(slot)
            if previous is None or previous["checked"] != checked:
                at = now
                activity = info.get("last_activity")
                if previous is not None and activity and previous["at"] < activity <= now:
                    at = activity  # the save knows when the latest check happened
                conn.execute("INSERT INTO game_progress(game_id, slot, at, checked) VALUES (?,?,?,?)",
                             (game_id, slot, at, checked))
                # The game began with its first check, if the manager saw it go from none to some
                if not first_look and checked_before == 0 and checked > 0 and (started is None or at < started):
                    started = at
            at_goal = info.get("status") == STATUS_GOAL
            if at_goal and player["goal_at"] is None:
                conn.execute("UPDATE game_slots SET goal_at = ? WHERE game_id = ? AND slot = ?", (now, game_id, slot))
                new_goal = True
            elif not at_goal and player["goal_at"] is not None:  # a restored backup from before the goal
                conn.execute("UPDATE game_slots SET goal_at = NULL WHERE game_id = ? AND slot = ?", (game_id, slot))
        if started != game["play_started_at"]:
            conn.execute("UPDATE games SET play_started_at = ? WHERE id = ?", (started, game_id))
        everyone = all(state["slots"].get(p["slot"], {}).get("status") == STATUS_GOAL for p in players)
        # Only a new goal finishes a game, so one an administrator took back out of the archive stays out
        finished = everyone and game["finished_at"] is None and (new_goal or first_look)
        if finished:
            _finish(conn, game_id, now)
    return finished


def finish(game_id: int, finished: bool) -> None:
    """Moves a game to the archive by hand, or back out of it."""
    with db.connection() as conn:
        if finished:
            if conn.execute("SELECT finished_at FROM games WHERE id = ?", (game_id,)).fetchone()[0] is None:
                _finish(conn, game_id, time.time())
        else:
            conn.execute("UPDATE games SET finished_at = NULL WHERE id = ?", (game_id,))


def _finish(conn: Any, game_id: int, now: float) -> None:
    conn.execute("UPDATE games SET finished_at = ? WHERE id = ?", (now, game_id))
    if conn.execute("SELECT share_spoiler_when_done FROM games WHERE id = ?", (game_id,)).fetchone()[0]:
        conn.execute("UPDATE game_files SET public = 1 WHERE game_id = ? AND kind = 'spoiler'", (game_id,))


def forget(game_id: int) -> None:
    """After the progress was reset: the game starts over."""
    with db.connection() as conn:
        conn.execute("DELETE FROM game_progress WHERE game_id = ?", (game_id,))
        conn.execute("UPDATE game_slots SET goal_at = NULL WHERE game_id = ?", (game_id,))
        conn.execute("UPDATE games SET play_started_at = NULL, finished_at = NULL WHERE id = ?", (game_id,))


def has_history(game_id: int) -> bool:
    return db.query_one("SELECT 1 FROM game_progress WHERE game_id = ? LIMIT 1", (game_id,)) is not None


def thin(points: list[tuple[float, int]], limit: int = MAX_POINTS) -> list[tuple[float, int]]:
    """At most `limit` points, evenly picked, always with the first and the last."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def series(game_id: int) -> dict:
    """The progress of every player slot over time, for the chart and the results."""
    game = db.query_one("SELECT play_started_at, finished_at FROM games WHERE id = ?", (game_id,))
    points: dict[int, list[tuple[float, int]]] = {}
    for row in db.query("SELECT slot, at, checked FROM game_progress WHERE game_id = ? ORDER BY slot, at", (game_id,)):
        points.setdefault(row["slot"], []).append((row["at"], row["checked"]))
    recorded_from = min((p[0][0] for p in points.values()), default=None)
    slots = []
    for s in db.query("SELECT s.slot, s.name, s.game, s.locations, s.goal_at, u.username AS owner FROM game_slots s "
                      "LEFT JOIN users u ON u.id = s.user_id WHERE s.game_id = ? AND s.type = 1 ORDER BY s.slot",
                      (game_id,)):
        slots.append({"slot": s["slot"], "name": s["name"], "game": s["game"], "owner": s["owner"],
                      "total": s["locations"], "goal_at": s["goal_at"],
                      "points": [list(p) for p in thin(points.get(s["slot"], []))]})
    return {"play_started_at": game["play_started_at"] if game else None,
            "finished_at": game["finished_at"] if game else None,
            "recorded_from": recorded_from, "now": time.time(), "slots": slots}
