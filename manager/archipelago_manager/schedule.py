"""Planned starts: a game opens at a set time, and a race starts on the second.

An administrator sets when a game starts (start_at). The server opens start_open_minutes earlier, so the players can
connect and get ready, and is announced on Discord like any start by a person. With start_countdown, the server counts
down in every client (Archipelago's /countdown) and says GO at start_at. That moment starts the race: the standings
time every player's goal from it.

start_stage says how far it got, so a restart of the manager neither opens a game twice nor skips one: None (planned),
'opened' (the server was started), 'started' (start_at has passed).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import db, notify
from .rooms import ROOMS, RoomError, player_list, slot_owners

log = logging.getLogger(__name__)

TICK_SECONDS = 2
COUNTDOWN_LEAD = 10  # seconds before the start the countdown begins; it counts what is left
MAX_OPEN_MINUTES = 24 * 60


def _set_stage(game_id: int, stage: str) -> None:
    db.execute("UPDATE games SET start_stage = ? WHERE id = ?", (stage, game_id))


async def tick(now: float | None = None) -> None:
    now = now or time.time()
    for game in db.query("SELECT id, name, start_at, start_open_minutes, start_countdown, start_stage FROM games "
                         "WHERE start_at IS NOT NULL AND (start_stage IS NULL OR start_stage = 'opened')"):
        stage = game["start_stage"]
        if stage is None and now >= game["start_at"] - game["start_open_minutes"] * 60:
            stage = "opened"
            _set_stage(game["id"], stage)
            if not ROOMS.is_active(game["id"]):  # an administrator may have opened it earlier
                try:
                    await ROOMS.start(game["id"], announce=True)
                except RoomError as e:
                    log.warning("Could not open game %s at its planned time: %s", game["id"], e)
                    notify.emit("game_crashed", notify.tr("Could not open {game}", game=game["name"]), str(e),
                                url=notify.link(f"/games/{game['id']}"), color="error", game_id=game["id"])
        if stage == "opened" and now >= game["start_at"] - COUNTDOWN_LEAD:
            left = round(game["start_at"] - now)
            if game["start_countdown"] and left > 0 and ROOMS.status(game["id"])["state"] == "running":
                with contextlib.suppress(RoomError):
                    await ROOMS.send_command(game["id"], f"/countdown {left}")
            _set_stage(game["id"], "started")


async def run_periodically() -> None:
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 - keep the clock running
            log.exception("Checking planned starts failed")
        await asyncio.sleep(TICK_SECONDS)


def announce(game_id: int) -> None:
    """Tells the players on Discord when the game starts."""
    game = db.query_one("SELECT name, start_at, start_open_minutes FROM games WHERE id = ?", (game_id,))
    if not game or not game["start_at"]:
        return
    start = game["start_at"]
    fields = [(notify.tr("Start"), f"{notify.timestamp(start, 'F')} ({notify.timestamp(start, 'R')})", False)]
    if game["start_open_minutes"]:
        fields.append((notify.tr("Server opens"), notify.timestamp(start - game["start_open_minutes"] * 60, "t"), True))
    fields.append((notify.tr("Players"), player_list(game_id)))
    notify.emit("game_online", notify.tr("Planned start: {game}", game=game["name"]),
                notify.tr("The server opens before the start, so everyone can connect and get ready.")
                if game["start_open_minutes"] else "",
                url=notify.link(f"/games/{game_id}"), fields=fields, color="info",
                ping="game_online", ping_users=slot_owners(game_id), game_id=game_id)
