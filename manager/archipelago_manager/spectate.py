"""Spectators: a link that shows a game without signing in, for viewers and as an overlay in a stream (watch.html).

Whoever runs a game makes the link (routes/games.py), and a new one ends the old one. The page shows how far every
player got, the standings with their race times, and with spectate_activity what happens in the multiworld: items,
hints, joins and goals, but never the chat. It shows nothing players connect with (address, password), and no user
names, only the slots' names as the game shows them.
"""
from __future__ import annotations

import secrets
import sqlite3

from . import activity, db

TOKEN_BYTES = 18
# What spectators see of the activity feed (activity.py): no chat, no server messages, no notes of the players
FEED_KINDS = {"item", "hint", "goal", "release", "collect", "join", "part"}
FEED_FIELDS = {"seq", "at", "kind", "player", "game", "finder", "receiver", "item", "location", "entrance", "status"}


def new_link(game_id: int) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.execute("UPDATE games SET spectate_token = ? WHERE id = ?", (token, game_id))
    return token


def end_link(game_id: int) -> None:
    db.execute("UPDATE games SET spectate_token = NULL WHERE id = ?", (game_id,))


def game_for(token: str) -> sqlite3.Row | None:
    """The game a spectator link shows, if it is one."""
    if not token or len(token) > 64:
        return None
    return db.query_one("SELECT * FROM games WHERE spectate_token = ?", (token,))


def feed(game_id: int, after: int = 0) -> dict:
    """The activity as spectators see it."""
    found = activity.entries(game_id, after)
    return {"entries": [{k: v for k, v in e.items() if k in FEED_FIELDS} for e in found["entries"]
                        if e["kind"] in FEED_KINDS], "last": found["last"]}
