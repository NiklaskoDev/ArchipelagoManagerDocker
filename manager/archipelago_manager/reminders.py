"""Reminders for players who stopped playing while the others play on.

A game reminds a player after remind_days days without playing (the server's remind_days setting, unless the game has
its own; 0 never), once someone else in the game has played since: when everybody left a game alone, nobody holds the
others up. Playing is checking a location (history.py), saying how one is doing (the slot's status) or taking the slot
over. The reminder is a Discord message (player_inactive), which mentions the player if they asked for it, says how many
hinted items of the others wait in their world and suggests handing the slot over; the dashboard and the game page show
it too. It comes again after as many days, at most MAX_REMINDERS times; playing again starts it over.

Players who reached their goal, said they are on a break or offer their slot to someone are not reminded, nor are the
players of a finished game or of one nobody began to play.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from . import apbridge, db, notify, settings as app_settings
from .config import CONFIG
from .rooms import ROOMS

log = logging.getLogger(__name__)

MAX_DAYS = app_settings.REMIND_MAX_DAYS
MAX_REMINDERS = 3
LISTED = 5  # players named who wait for hinted items; the rest are counted


def days_for(game: sqlite3.Row) -> int:
    return game["remind_days"] if game["remind_days"] is not None else app_settings.get_setting("remind_days")


def _last_checks(game_id: int) -> dict[int, float]:
    """When each slot last checked a location. The history notes where a slot stands when it first looks at the save,
    checks or not: a point without checks is no sign of playing."""
    return {r["slot"]: r["at"] for r in db.query("SELECT slot, MAX(at) AS at FROM game_progress "
                                                 "WHERE game_id = ? AND checked > 0 GROUP BY slot", (game_id,))}


def idle_since(game: sqlite3.Row, slot: sqlite3.Row, last_checks: dict[int, float] | None = None) -> float | None:
    """When the player of a slot last played: their latest check, their status, or when they took the slot."""
    if last_checks is None:
        last_checks = _last_checks(game["id"])
    times = (last_checks.get(slot["slot"]), slot["status_at"], slot["player_since"], game["play_started_at"])
    return max((at for at in times if at), default=None)


async def _waiting(game_id: int, slot: int) -> dict[int, int]:
    """Per player slot, how many of their hinted items wait in this slot's world, as of the server's save."""
    try:
        state = await ROOMS.save_state(game_id)
    except apbridge.APBridgeError:
        state = None
    counts: dict[int, int] = {}
    for receiving, finding, location, *_rest in (state or {}).get("hints", []):
        if finding == slot and receiving != slot and location not in state["checks"].get(slot, ()):
            counts[receiving] = counts.get(receiving, 0) + 1
    return counts


async def _remind(game: sqlite3.Row, slot: sqlite3.Row, since: float, now: float, names: dict[int, str]) -> None:
    waiting = await _waiting(game["id"], slot["slot"])
    fields = []
    if waiting:
        shown = sorted(waiting.items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))
        lines = [notify.tr("{player}: {n}", player=notify.md(names.get(receiver, "?")), n=n)
                 for receiver, n in shown[:LISTED]]
        if len(shown) > LISTED:
            lines.append(notify.tr("…and {n} more", n=len(shown) - LISTED))
        fields.append((notify.tr("Hinted items of others in their world"), "\n".join(lines)))
    text = notify.tr("{player} has not played {game} for {time}, and the others play on.",
                     player=notify.md(slot["name"]), game=notify.md(game["name"]), time=notify.duration(now - since))
    text += " " + notify.tr("If they cannot play on, they can hand their slot over on the game page.")
    notify.emit("player_inactive", notify.tr("Reminder for {player}", player=slot["name"]), text,
                url=notify.link(f"/games/{game['id']}"), fields=fields, color="warning",
                ping="player_inactive", ping_users={slot["user_id"]}, game_id=game["id"])


async def check(now: float | None = None) -> None:
    now = now or time.time()
    day = CONFIG.reminder_day_seconds
    for game in db.query("SELECT * FROM games WHERE finished_at IS NULL AND play_started_at IS NOT NULL"):
        days = days_for(game)
        if not days:
            continue
        period = days * day
        slots = db.query("SELECT * FROM game_slots WHERE game_id = ? AND type = 1", (game["id"],))
        last = _last_checks(game["id"])
        names = {s["slot"]: s["name"] for s in slots}
        for slot in slots:
            if slot["user_id"] is None or slot["goal_at"] is not None or slot["player_status"] == "away" \
                    or slot["handover_at"] is not None:
                continue
            since = idle_since(game, slot, last)
            reminded_at, count = slot["reminded_at"], slot["reminders"]
            if reminded_at and since and since > reminded_at:  # played again since the last reminder
                db.execute("UPDATE game_slots SET reminded_at = NULL, reminders = 0 WHERE game_id = ? AND slot = ?",
                           (game["id"], slot["slot"]))
                reminded_at, count = None, 0
            if since is None or count >= MAX_REMINDERS or now < max(since, reminded_at or 0) + period:
                continue
            # someone else played after this player stopped: they are the ones holding the game up
            others = max((at for other in slots if other["slot"] != slot["slot"]
                          for at in (last.get(other["slot"]), other["goal_at"]) if at), default=0)
            if others <= since:
                continue
            db.execute("UPDATE game_slots SET reminded_at = ?, reminders = ? WHERE game_id = ? AND slot = ?",
                       (now, count + 1, game["id"], slot["slot"]))
            await _remind(game, slot, since, now, names)


async def run_periodically() -> None:
    interval = max(1, min(600, CONFIG.reminder_day_seconds // 144))
    while True:
        await asyncio.sleep(interval)
        try:
            await check()
        except Exception:  # noqa: BLE001 - keep reminding
            log.exception("Reminding players failed")
