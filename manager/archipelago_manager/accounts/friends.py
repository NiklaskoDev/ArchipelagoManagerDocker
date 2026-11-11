"""Friends: one player asks another, and once that one accepts they are friends. Friends can share configs with each
other alone (configs/sharing.py) and put each other in the games they generate (generation/jobs.py).

One row of the friends table stands for a pair of people, in the direction it was asked; accepted_at is NULL while the
request waits. Ending a friendship, turning a request down and taking one back all remove that row."""
from __future__ import annotations

import sqlite3

from ..core import db

MAX_FRIENDS = 200  # friends and requests, either way, that one person can have
MAX_WAITING = 30   # requests of one person that wait for an answer

# The ids of a user's friends, to use in SQL as "... IN " + FRIEND_IDS, with the user's id twice among the parameters
FRIEND_IDS = ("(SELECT friend_id FROM friends WHERE user_id = ? AND accepted_at IS NOT NULL "
              "UNION ALL SELECT user_id FROM friends WHERE friend_id = ? AND accepted_at IS NOT NULL)")


class FriendError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _pair(conn: sqlite3.Connection | None, a: int, b: int) -> sqlite3.Row | None:
    sql = "SELECT * FROM friends WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)"
    params = (a, b, b, a)
    return conn.execute(sql, params).fetchone() if conn else db.query_one(sql, params)


def are_friends(a: int, b: int) -> bool:
    row = _pair(None, a, b)
    return bool(row and row["accepted_at"] is not None)


def friend_ids(user_id: int) -> set[int]:
    """The user's friends whose accounts are not disabled."""
    rows = db.query(f"SELECT id FROM users WHERE disabled = 0 AND id IN {FRIEND_IDS}", (user_id, user_id))
    return {r["id"] for r in rows}


def overview(user_id: int) -> dict:
    """The user's friends, the requests that wait for them and for others, and people they played with who are not
    their friends yet. People whose accounts are disabled are left out."""
    rows = db.query(
        "SELECT f.*, u.id AS other_id, u.username, u.discord_avatar, u.discord_id FROM friends f "
        "JOIN users u ON u.id = CASE WHEN f.user_id = ? THEN f.friend_id ELSE f.user_id END "
        "WHERE (f.user_id = ? OR f.friend_id = ?) AND u.disabled = 0 ORDER BY u.username COLLATE NOCASE",
        (user_id, user_id, user_id))
    friends, incoming, outgoing = [], [], []
    for r in rows:
        person = {"id": r["other_id"], "username": r["username"]}
        if r["accepted_at"] is not None:
            friends.append({**person, "since": r["accepted_at"]})
        elif r["friend_id"] == user_id:
            incoming.append({**person, "asked_at": r["created_at"]})
        else:
            outgoing.append({**person, "asked_at": r["created_at"]})
    if friends:
        # what each friend shares with the user, and the games they are in together
        ids = [f["id"] for f in friends]
        marks = ",".join("?" * len(ids))
        shared = {r["user_id"]: r["n"] for r in db.query(
            f"SELECT c.user_id, COUNT(*) AS n FROM configs c WHERE c.user_id IN ({marks}) AND c.shared_at IS NOT NULL "
            "AND (c.share_with IN ('everyone', 'friends') OR EXISTS (SELECT 1 FROM config_shares s "
            "WHERE s.config_id = c.id AND s.user_id = ?)) GROUP BY c.user_id", (*ids, user_id))}
        together = _games_together(user_id, ids)
        for f in friends:
            f["shared_configs"] = shared.get(f["id"], 0)
            f["games_together"] = together.get(f["id"], 0)
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing,
            "suggestions": suggestions(user_id, {r["other_id"] for r in rows})}


def _games_together(user_id: int, others: list[int] | None = None) -> dict[int, int]:
    """For the people who own a slot in a game the user owns one in too: in how many such games."""
    rows = db.query(
        "SELECT o.user_id, COUNT(DISTINCT o.game_id) AS n FROM game_slots o "
        "WHERE o.user_id IS NOT NULL AND o.user_id != ? "
        "AND o.game_id IN (SELECT game_id FROM game_slots WHERE user_id = ?) GROUP BY o.user_id", (user_id, user_id))
    wanted = set(others) if others is not None else None
    return {r["user_id"]: r["n"] for r in rows if wanted is None or r["user_id"] in wanted}


def suggestions(user_id: int, known: set[int], limit: int = 8) -> list[dict]:
    """People the user played with (they own slots in the same games), most games together first, who are not their
    friends and have no request waiting either way."""
    together = {uid: n for uid, n in _games_together(user_id).items() if uid not in known}
    if not together:
        return []
    marks = ",".join("?" * len(together))
    names = {r["id"]: r["username"] for r in db.query(
        f"SELECT id, username FROM users WHERE disabled = 0 AND id IN ({marks})", tuple(together))}
    people = [{"id": uid, "username": names[uid], "games_together": n} for uid, n in together.items() if uid in names]
    people.sort(key=lambda p: (-p["games_together"], p["username"].casefold()))
    return people[:limit]


def ask(user_id: int, username: str) -> tuple[str, sqlite3.Row]:
    """Asks the user called `username` to be friends. When they had asked the user already, that request is accepted
    instead. Returns what happened ("asked" or "accepted") and the other user."""
    username = username.strip()
    with db.connection() as conn:
        other = conn.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (username,)).fetchone()
        if not other:
            raise FriendError(f"There is no user called '{username}'", 404)
        if other["id"] == user_id:
            raise FriendError("You cannot add yourself as a friend")
        row = _pair(conn, user_id, other["id"])
        if row is not None:
            if row["accepted_at"] is not None:
                raise FriendError(f"You and {other['username']} are friends already", 409)
            if row["user_id"] == user_id:
                raise FriendError(f"You asked {other['username']} already; they have not answered yet", 409)
            conn.execute("UPDATE friends SET accepted_at = ? WHERE user_id = ? AND friend_id = ?",
                         (db.now(), other["id"], user_id))
            return "accepted", other
        def count(person: int) -> int:
            return conn.execute("SELECT COUNT(*) FROM friends WHERE user_id = ? OR friend_id = ?",
                                (person, person)).fetchone()[0]
        if count(user_id) >= MAX_FRIENDS:
            raise FriendError(f"You have {MAX_FRIENDS} friends and requests, the most one person can have", 409)
        if count(other["id"]) >= MAX_FRIENDS:
            raise FriendError(f"{other['username']} has {MAX_FRIENDS} friends and requests, the most one person can "
                              "have", 409)
        waiting = conn.execute("SELECT COUNT(*) FROM friends WHERE user_id = ? AND accepted_at IS NULL",
                               (user_id,)).fetchone()[0]
        if waiting >= MAX_WAITING:
            raise FriendError(f"{MAX_WAITING} of your friend requests wait for an answer. Take some back first.", 409)
        conn.execute("INSERT INTO friends(user_id, friend_id, created_at) VALUES (?, ?, ?)",
                     (user_id, other["id"], db.now()))
        return "asked", other


def accept(user_id: int, other_id: int) -> None:
    with db.connection() as conn:
        updated = conn.execute("UPDATE friends SET accepted_at = ? WHERE user_id = ? AND friend_id = ? "
                               "AND accepted_at IS NULL", (db.now(), other_id, user_id)).rowcount
    if not updated:
        raise FriendError("That friend request no longer exists", 404)


def remove(user_id: int, other_id: int) -> str:
    """Ends a friendship, turns down a request or takes one back. Configs shared with the other one by name are no
    longer, either way. Returns which of them it was ("friend", "declined" or "withdrawn")."""
    with db.connection() as conn:
        row = _pair(conn, user_id, other_id)
        if row is None:
            raise FriendError("You are not friends, and there is no request between you", 404)
        conn.execute("DELETE FROM friends WHERE user_id = ? AND friend_id = ?", (row["user_id"], row["friend_id"]))
        conn.execute("DELETE FROM config_shares WHERE (user_id = ? AND config_id IN (SELECT id FROM configs "
                     "WHERE user_id = ?)) OR (user_id = ? AND config_id IN (SELECT id FROM configs WHERE user_id = ?))",
                     (other_id, user_id, user_id, other_id))
        # a config that was shared with that one alone is shared with nobody now: no longer shared at all
        conn.execute("UPDATE configs SET shared_at = NULL WHERE user_id IN (?, ?) AND shared_at IS NOT NULL "
                     "AND share_with = 'chosen' AND NOT EXISTS (SELECT 1 FROM config_shares s "
                     "WHERE s.config_id = configs.id)", (user_id, other_id))
    if row["accepted_at"] is not None:
        return "friend"
    return "withdrawn" if row["user_id"] == user_id else "declined"
