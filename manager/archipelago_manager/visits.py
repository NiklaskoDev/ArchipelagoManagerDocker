"""What happened in a game while a player was away, shown at the top of the game page when they come back.

The game page reports a visit when it opens, and again every minute while it is open and in view (POST
/api/games/{id}/visit). Coming back after AWAY seconds without one starts a summary from the last visit; it stays, however
often the page is opened, until the player dismisses it. It sums up what concerns the player's slots: the items others
sent them, open hints between them and others, goals, releases and collects, who got stuck, and how much was said in
the chat. It is read from the activity feed (activity.py), so it reaches back as far as the feed's latest entries.
"""
from __future__ import annotations

import time

from . import activity, db

AWAY = 10 * 60
LISTED = 20  # items and hints listed by name; the rest are only counted


def visit(game_id: int, user_id: int, *, dismiss: bool = False, now: float | None = None) -> float | None:
    """Notes a look at the game page. Returns where the summary starts, or None when there is nothing to sum up: on the
    first visit, after a dismissal, and while the player has not been away since."""
    now = now or time.time()
    row = db.query_one("SELECT seen_at, summary_from FROM game_visits WHERE game_id = ? AND user_id = ?",
                       (game_id, user_id))
    start = None
    if row is not None and not dismiss:
        start = row["seen_at"] if now - row["seen_at"] > AWAY else row["summary_from"]
    db.execute("INSERT INTO game_visits(game_id, user_id, seen_at, summary_from) VALUES (?,?,?,?) "
               "ON CONFLICT(game_id, user_id) DO UPDATE SET seen_at = excluded.seen_at, "
               "summary_from = excluded.summary_from", (game_id, user_id, now, start))
    return start


def _one_of(name: str | None, slots: set[str]) -> bool:
    """Whether a name in the server's log is one of these slots: the server shows "Alias (Name)" for a player who set
    an alias."""
    return bool(name) and (name in slots or any(name.endswith(f" ({slot})") for slot in slots))


def summary(game_id: int, slots: set[str], since: float) -> dict | None:
    """What happened after `since` that concerns the player with these slots (their names), or None if nothing did."""
    feed = activity.entries(game_id)["entries"]
    new = [e for e in feed if e["at"] > since]
    mine = lambda name: _one_of(name, slots)  # noqa: E731

    received = [e for e in new if e["kind"] == "item" and mine(e["receiver"]) and not mine(e["finder"])]
    # Hints between the player and others: their item somewhere else, or someone's item waiting in their world. The
    # last word on each counts, and one whose location has been checked since is found.
    hints: dict[tuple, dict] = {}
    for e in new:
        if e["kind"] == "hint" and mine(e["receiver"]) != mine(e["finder"]):
            hints[e["receiver"], e["item"], e["location"], e["finder"]] = e
    checked = {(e["finder"], e["location"]) for e in new if e["kind"] == "item"}
    open_hints = [e for e in hints.values() if e["status"] != "found" and (e["finder"], e["location"]) not in checked]
    goals = [e["player"] for e in new if e["kind"] == "goal" and not mine(e["player"])]
    gave = [{"player": e["player"], "kind": e["kind"]} for e in new
            if e["kind"] in ("release", "collect") and not mine(e["player"])]
    chat = sum(1 for e in new if e["kind"] == "chat" and not mine(e["player"]))
    # who got stuck meanwhile and still is
    stuck_now = {r["name"]: r for r in db.query("SELECT name, status_note, status_at FROM game_slots "
                                                "WHERE game_id = ? AND player_status = 'stuck' AND goal_at IS NULL",
                                                (game_id,))}
    stuck = [{"player": name, "note": row["status_note"], "at": row["status_at"]} for name, row in stuck_now.items()
             if name not in slots and row["status_at"] and row["status_at"] > since]

    if not (received or open_hints or goals or gave or chat or stuck):
        return None
    return {
        "since": since,
        # the feed keeps its latest entries only: the oldest one kept is younger than the visit
        "partial": len(feed) >= activity.KEEP and feed[0]["at"] > since,
        "received": {"count": len(received), "items": [{"item": e["item"], "finder": e["finder"],
                                                        "location": e["location"]} for e in received[-LISTED:]]},
        "hints": [{"item": e["item"], "receiver": e["receiver"], "finder": e["finder"], "location": e["location"],
                   "entrance": e.get("entrance"), "in_my_world": mine(e["finder"])} for e in open_hints[-LISTED:]],
        "hint_count": len(open_hints),
        "goals": goals, "released": gave, "chat": chat, "stuck": stuck,
    }
