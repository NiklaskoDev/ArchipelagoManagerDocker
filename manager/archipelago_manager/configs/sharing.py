"""Who sees a config its owner shares: every player ('everyone'), the owner's friends ('friends'), or the friends the
owner chose ('chosen', listed in config_shares). Whoever sees one can copy it into their own library; a friend who sees
one can also generate a game with it, which puts its owner in that game (generation/jobs.py)."""
from __future__ import annotations

import sqlite3

from ..accounts import friends
from ..core import db

AUDIENCES = ("everyone", "friends", "chosen")


class ShareError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def visible_sql(user_id: int, alias: str = "c") -> tuple[str, tuple]:
    """An SQL condition on the configs table (as `alias`, joined with its owner as `u`) that holds for the configs
    shared with the user, theirs included."""
    c = alias
    return (f"({c}.shared_at IS NOT NULL AND u.disabled = 0 AND ({c}.user_id = ? OR {c}.share_with = 'everyone' "
            f"OR ({c}.user_id IN {friends.FRIEND_IDS} AND ({c}.share_with = 'friends' OR EXISTS ("
            f"SELECT 1 FROM config_shares s WHERE s.config_id = {c}.id AND s.user_id = ?)))))",
            (user_id, user_id, user_id, user_id))


def friends_sql(user_id: int, alias: str = "c") -> tuple[str, tuple]:
    """Like visible_sql, for the configs the user's friends share with them (by any of the three ways)."""
    c = alias
    return (f"({c}.shared_at IS NOT NULL AND u.disabled = 0 AND {c}.user_id IN {friends.FRIEND_IDS} AND "
            f"({c}.share_with IN ('everyone', 'friends') OR EXISTS (SELECT 1 FROM config_shares s "
            f"WHERE s.config_id = {c}.id AND s.user_id = ?)))", (user_id, user_id, user_id))


def shared_with(row: sqlite3.Row, user_id: int, *, owner_disabled: bool) -> bool:
    """Whether another player's config is shared with the user."""
    if row["shared_at"] is None or owner_disabled:
        return False
    if row["share_with"] == "everyone":
        return True
    if not friends.are_friends(row["user_id"], user_id):
        return False
    return row["share_with"] == "friends" or db.query_one(
        "SELECT 1 FROM config_shares WHERE config_id = ? AND user_id = ?", (row["id"], user_id)) is not None


def from_friend(row: sqlite3.Row, user_id: int) -> bool:
    """Whether a config the user sees (shared_with) is one of a friend's, which they can generate with."""
    return friends.are_friends(row["user_id"], user_id)


def chosen_friends(config_ids: list[int]) -> dict[int, list[dict]]:
    """The friends each of these configs is shared with by name."""
    if not config_ids:
        return {}
    result: dict[int, list[dict]] = {}
    for start in range(0, len(config_ids), 500):  # under SQLite's limit of parameters
        part = config_ids[start:start + 500]
        for r in db.query(f"SELECT s.config_id, u.id, u.username FROM config_shares s JOIN users u ON u.id = s.user_id "
                          f"WHERE s.config_id IN ({','.join('?' * len(part))}) ORDER BY u.username COLLATE NOCASE",
                          tuple(part)):
            result.setdefault(r["config_id"], []).append({"id": r["id"], "username": r["username"]})
    return result


def set_sharing(config_id: int, owner_id: int, shared: bool, share_with: str = "everyone",
                friend_ids: list[int] | None = None) -> None:
    """Shares a config with `share_with` (and for 'chosen', the friends in `friend_ids`), or stops sharing it. A config
    that was shared keeps the time it was first shared, whoever it is shared with now."""
    if not shared:
        with db.connection() as conn:
            conn.execute("UPDATE configs SET shared_at = NULL WHERE id = ?", (config_id,))
            conn.execute("DELETE FROM config_shares WHERE config_id = ?", (config_id,))
        return
    if share_with not in AUDIENCES:
        raise ShareError(f"Share with one of: {', '.join(AUDIENCES)}")
    chosen = sorted(set(friend_ids or [])) if share_with == "chosen" else []
    if share_with == "chosen":
        if not chosen:
            raise ShareError("Choose at least one friend to share it with")
        strangers = set(chosen) - friends.friend_ids(owner_id)
        if strangers:
            raise ShareError("You can share a config by name only with your friends")
    with db.connection() as conn:
        conn.execute("UPDATE configs SET shared_at = COALESCE(shared_at, ?), share_with = ? WHERE id = ?",
                     (db.now(), share_with, config_id))
        conn.execute("DELETE FROM config_shares WHERE config_id = ?", (config_id,))
        conn.executemany("INSERT INTO config_shares(config_id, user_id) VALUES (?, ?)",
                         [(config_id, uid) for uid in chosen])
