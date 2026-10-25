"""Hosted games: upload, settings, start/stop, slots and the files players download."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import (activity, apbridge, audit, backups, db, gamepaths, gamestore, history, notify, reminders, schedule,
               spectate, versions, visits, settings as app_settings, textclient)
from ..config import CONFIG
from ..rooms import ROOMS, STATUS_GOAL, RoomError, mask_secrets, sleep_minutes, slot_rules, valid_password
from ..security import current_user, is_admin, manages, organizer_user
from ..textclient import TEXT_CLIENTS

router = APIRouter(prefix="/api")

# ClientStatus values from NetUtils (besides STATUS_GOAL)
STATUS_ONLINE = (5, 10, 20)
# Server options that cannot be changed on a running server (everything else is applied with /option)
RESTART_OPTIONS = {"loglevel", "use_embedded_options"}


# ----------------------------------------------------------------------------------------------------------------------
# access rules
# ----------------------------------------------------------------------------------------------------------------------
def is_participant(game_id: int, user_id: int) -> bool:
    return db.query_one("SELECT 1 FROM game_slots WHERE game_id = ? AND user_id = ?", (game_id, user_id)) is not None


def is_offered_to(game_id: int, user_id: int) -> bool:
    """Whether a player of the game offers their slot to this user (set_handover): they see the game to take it."""
    return db.query_one("SELECT 1 FROM game_slots WHERE game_id = ? AND handover_at IS NOT NULL AND handover_to = ?",
                        (game_id, user_id)) is not None


def get_game(game_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM games WHERE id = ?", (game_id,))
    if not row:
        raise HTTPException(404, "No such game")
    if not (manages(row, user) or row["visibility"] == "all" or is_participant(game_id, user["id"])
            or is_offered_to(game_id, user["id"])):
        raise HTTPException(404, "No such game")
    return row


def managed_game(game_id: int, user: sqlite3.Row) -> sqlite3.Row:
    """A game this user runs: an administrator, or the organizer who added or generated it."""
    row = get_game(game_id, user)
    if not manages(row, user):
        raise HTTPException(403, "Only the organizer of this game and administrators can do this")
    return row


def can_control(game: sqlite3.Row, user: sqlite3.Row) -> bool:
    return manages(game, user) or (app_settings.get_setting("users_can_start_rooms")
                                   and is_participant(game["id"], user["id"]))


def file_visible(f: sqlite3.Row, game: sqlite3.Row, user: sqlite3.Row) -> bool:
    if manages(game, user):
        return True
    if f["public"]:
        return True
    if f["kind"] == "patch" and f["slot"] is not None:
        slot = db.query_one("SELECT user_id FROM game_slots WHERE game_id = ? AND slot = ?", (game["id"], f["slot"]))
        return bool(slot and slot["user_id"] == user["id"])
    return False


# ----------------------------------------------------------------------------------------------------------------------
# serialization
# ----------------------------------------------------------------------------------------------------------------------
def public_host(request: Request) -> str:
    return app_settings.get_setting("public_host") or request.url.hostname or "localhost"


def _slots_for(game_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    if not game_ids:
        return {}
    marks = ",".join("?" * len(game_ids))
    rows = db.query(f"SELECT s.*, u.username AS owner, o.username AS handover_to_name FROM game_slots s "
                    f"LEFT JOIN users u ON u.id = s.user_id LEFT JOIN users o ON o.id = s.handover_to "
                    f"WHERE s.game_id IN ({marks}) ORDER BY s.game_id, s.slot", game_ids)
    grouped: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        grouped.setdefault(r["game_id"], []).append(r)
    return grouped


def handover_json(s: sqlite3.Row, game: sqlite3.Row, user: sqlite3.Row, my_slots: list) -> dict | None:
    """A slot its player hands over: to whom (None: whoever takes it), and whether this user can take it."""
    if s["handover_at"] is None or s["type"] != 1 or s["user_id"] is None:
        return None
    for_me = s["handover_to"] is None or s["handover_to"] == user["id"]
    return {"at": s["handover_at"], "from": s["owner"], "to": s["handover_to_name"],
            "to_me": s["handover_to"] == user["id"], "note": s["handover_note"],
            # with "one slot each", someone who already has a slot cannot take another one
            "can_accept": for_me and s["user_id"] != user["id"] and not (game["single_claim"] and my_slots)}


def game_summary(row: sqlite3.Row, slots: list[sqlite3.Row], user: sqlite3.Row, host: str) -> dict:
    runtime = ROOMS.status(row["id"])
    if runtime["state"] == "stopped" and ROOMS.is_asleep(row):
        runtime["state"] = "sleeping"
    mine = [s for s in slots if s["user_id"] == user["id"]]
    games = db.jloads(row["games"], [])
    players = [s for s in slots if s["type"] == 1]
    return {
        "id": row["id"], "name": row["name"], "seed_name": row["seed_name"], "source": row["source"],
        "created_at": row["created_at"], "race_mode": bool(row["race_mode"]),
        "state": runtime["state"], "error": runtime["error"], "started_at": runtime["started_at"],
        "desired_state": row["desired_state"],
        "sleep_minutes": sleep_minutes(row),  # 0: never falls asleep
        "port": row["port"], "address": f"{host}:{row['port']}" if row["port"] else None,
        "has_password": bool(row["password"]),
        "games": games, "missing_games": apbridge.missing_games(games, row["ap_version"]),
        # the Archipelago version the server runs (players need a client that goes with it); own: not the image's
        "ap_version": apbridge.ap_version(row["ap_version"]), "ap_version_own": row["ap_version"],
        "ap_version_missing": bool(row["ap_version"] and not versions.available(row["ap_version"])),
        "player_count": len(players), "claimed_count": sum(1 for s in players if s["user_id"] is not None),
        "visibility": row["visibility"], "claimable": bool(row["claimable"]),
        "single_claim": bool(row["single_claim"]),
        "play_started_at": row["play_started_at"], "finished_at": row["finished_at"],
        # a planned start: when, when the server opens, whether a countdown runs, and how far it got
        "start": {"at": row["start_at"], "opens_at": row["start_at"] - row["start_open_minutes"] * 60,
                  "open_minutes": row["start_open_minutes"], "countdown": bool(row["start_countdown"]),
                  "stage": row["start_stage"] or "planned"} if row["start_at"] else None,
        "goal_count": sum(1 for s in players if s["goal_at"] is not None),
        # with, for a slot whose player was reminded that the others wait (reminders.py), since when they did not play
        "my_slots": [{"slot": s["slot"], "name": s["name"], "game": s["game"],
                      "idle_since": reminders.idle_since(row, s) if s["reminders"] else None} for s in mine],
        # slots offered to this user, or to whoever takes them
        "offered_slots": [{"slot": s["slot"], "name": s["name"], "game": s["game"], **handover}
                          for s in slots if (handover := handover_json(s, row, user, mine)) and handover["can_accept"]],
        "my_file_count": db.query_one(
            "SELECT COUNT(*) AS n FROM game_files f JOIN game_slots s ON s.game_id = f.game_id AND s.slot = f.slot "
            "WHERE f.game_id = ? AND f.kind = 'patch' AND s.user_id = ?", (row["id"], user["id"]))["n"] if mine else 0,
        "can_control": bool(can_control(row, user)),
        "can_manage": manages(row, user),
    }


def status_label(status: int, running: bool) -> str:
    if status == STATUS_GOAL:
        return "goal"
    return "online" if running and status in STATUS_ONLINE else "offline"


# ----------------------------------------------------------------------------------------------------------------------
# list / create
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/games")
def list_games(request: Request, user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    if is_admin(user):
        rows = db.query("SELECT * FROM games ORDER BY id DESC")
    else:  # organizers also see the games they run
        rows = db.query("SELECT g.* FROM games g WHERE g.visibility = 'all' OR (? AND g.created_by = ?) OR EXISTS "
                        "(SELECT 1 FROM game_slots s WHERE s.game_id = g.id AND (s.user_id = ? OR "
                        "(s.handover_at IS NOT NULL AND s.handover_to = ?))) ORDER BY g.id DESC",
                        (user["role"] == "organizer", user["id"], user["id"], user["id"]))
    slots = _slots_for([r["id"] for r in rows])
    host = public_host(request)
    return [game_summary(r, slots.get(r["id"], []), user, host) for r in rows]


@router.post("/games")
async def upload_game(request: Request, file: UploadFile = File(...), name: str = Form(""),
                      start: bool = Form(False), ap_version: str = Form(""),
                      user: sqlite3.Row = Depends(organizer_user)) -> dict:
    try:
        version = versions.check_choice(ap_version)
    except versions.VersionError as e:
        raise HTTPException(e.status, e.message) from None
    tmp = CONFIG.tmp_dir / f"upload-{db.now():.6f}-{user['id']}.bin"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        try:
            game_id = await gamestore.import_game(tmp, file.filename or "", name=name, created_by=user["id"],
                                                  ap_version=version)
        except gamestore.GameImportError as e:
            raise HTTPException(400, str(e)) from None
    finally:
        tmp.unlink(missing_ok=True)
    audit.note(request, target=("game", game_id, db.query_one("SELECT name FROM games WHERE id = ?", (game_id,))[0]))
    start_error = None
    if start:
        try:
            await ROOMS.start(game_id, announce=True)
        except RoomError as e:
            start_error = str(e)
    return {**await _detail(game_id, user, request), "start_error": start_error}


# ----------------------------------------------------------------------------------------------------------------------
# detail
# ----------------------------------------------------------------------------------------------------------------------
async def _detail(game_id: int, user: sqlite3.Row, request: Request) -> dict:
    row = get_game(game_id, user)
    manager = manages(row, user)
    slots = _slots_for([game_id]).get(game_id, [])
    summary = game_summary(row, slots, user, public_host(request))
    running = summary["state"] in ("starting", "running")
    progress = await ROOMS.progress(game_id)

    files = db.query("SELECT * FROM game_files WHERE game_id = ? ORDER BY filename COLLATE NOCASE", (game_id,))
    visible_files = [f for f in files if file_visible(f, row, user)]
    files_by_slot: dict[int, list[dict]] = {}
    for f in visible_files:
        if f["slot"] is not None and f["kind"] == "patch":
            files_by_slot.setdefault(f["slot"], []).append(_file_json(f))

    # With "one slot each", someone who already has a slot cannot claim another one.
    may_claim = bool(row["claimable"]) and not (row["single_claim"] and summary["my_slots"])
    slot_items = []
    for s in slots:
        p = progress.get(s["slot"], {})
        slot_items.append({
            "slot": s["slot"], "name": s["name"], "game": s["game"], "type": s["type"],
            "locations": s["locations"], "user_id": s["user_id"], "owner": s["owner"],
            "mine": s["user_id"] == user["id"],
            "claimable": may_claim and s["user_id"] is None and s["type"] == 1,
            "checked": p.get("checked", 0), "hints_used": p.get("hints_used", 0), "goal_at": s["goal_at"],
            "last_activity": p.get("last_activity"),
            "status": status_label(p.get("status", 0), running) if s["type"] == 1 else None,
            # what the player says about how they are doing (set_slot_status)
            "player_status": s["player_status"], "status_note": s["status_note"], "status_at": s["status_at"],
            "handover": handover_json(s, row, user, summary["my_slots"]),
            "files": files_by_slot.get(s["slot"], []),
        })
    shared = [_file_json(f) for f in visible_files if not (f["slot"] is not None and f["kind"] == "patch")]
    total = sum(s["locations"] for s in slots if s["type"] == 1)
    checked = sum(i["checked"] for i in slot_items if i["type"] == 1)
    # The generation job knows the configs and options of the game, to generate it again (jobs are pruned eventually)
    job = db.query_one("SELECT created_by FROM jobs WHERE id = ?", (row["job_id"],)) if row["job_id"] else None

    detail = {
        **summary,
        "slots": slot_items, "shared_files": shared,
        "progress": {"checked": checked, "total": total},
        "password": row["password"] if (manager or summary["my_slots"]) else None,
        "options": db.jloads(row["options"], {}),
        "generator_version": row["generator_version"], "min_server_version": row["min_server_version"],
        "job_id": row["job_id"],
        "job_available": bool(job and (manager or job["created_by"] == user["id"])),
        "share_spoiler_when_done": bool(row["share_spoiler_when_done"]),
    }
    if manager:
        detail["notes"] = row["notes"]  # for whoever runs the game only
        detail["server_password"] = row["server_password"]
        detail["has_save"] = gamepaths.save_path(game_id).is_file()
        # like a password: whoever has it can post to the channel
        detail["discord_webhook_url"] = row["discord_webhook_url"] or ""
        detail["notify_status"] = notify.game_status(game_id)
        # the link for spectators (spectate.py), and when players are reminded that the others wait for them
        detail["spectate"] = {"token": row["spectate_token"], "activity": bool(row["spectate_activity"])} \
            if row["spectate_token"] else None
        detail["remind_days"] = row["remind_days"]  # None: the server's default
        detail["remind_days_default"] = app_settings.get_setting("remind_days")
    return detail


def _file_json(f: sqlite3.Row) -> dict:
    return {"id": f["id"], "filename": f["filename"], "size": f["size"], "kind": f["kind"], "slot": f["slot"],
            "public": bool(f["public"])}


@router.get("/games/{game_id}")
async def game_detail(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------------------------------------------------------
class GamePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    port: int | None = None
    password: str | None = Field(default=None, max_length=64)
    server_password: str | None = Field(default=None, max_length=64)
    options: dict | None = None
    visibility: str | None = None
    claimable: bool | None = None
    single_claim: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)
    share_spoiler_when_done: bool | None = None
    finished: bool | None = None
    discord_webhook_url: str | None = Field(default=None, max_length=512)
    start_at: float | None = None  # null takes a planned start back
    start_open_minutes: int | None = None
    start_countdown: bool | None = None
    ap_version: str | None = Field(default=None, max_length=20)  # "": the image's
    spectate_activity: bool | None = None
    remind_days: int | None = None  # None (sent as null): the server's default; 0: never


def _check_webhook(url: str, user: sqlite3.Row) -> None:
    if not app_settings.valid_url(url):
        raise HTTPException(400, "The webhook address must start with http:// or https://")
    # The manager posts to the address it is given: an organizer's game gets Discord's webhooks, not any address
    if not is_admin(user) and not app_settings.discord_webhook(url):
        raise HTTPException(400, "Enter the address of a Discord webhook (https://discord.com/api/webhooks/…)")


@router.patch("/games/{game_id}")
async def patch_game(game_id: int, body: GamePatch, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = managed_game(game_id, user)
    changes: dict[str, object] = {}
    given = body.model_fields_set
    restart_required = False

    if "name" in given and body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "The name cannot be empty")
        changes["name"] = body.name.strip()
    if "port" in given and body.port is not None:
        if not (1024 <= body.port <= 65535):
            raise HTTPException(400, "Port must be between 1024 and 65535")
        # the ports published for games; an administrator may know of others
        if not is_admin(user) and not (CONFIG.port_start <= body.port <= CONFIG.port_end):
            raise HTTPException(400, f"Pick a port between {CONFIG.port_start} and {CONFIG.port_end}")
        if db.query_one("SELECT 1 FROM games WHERE port = ? AND id != ?", (body.port, game_id)):
            raise HTTPException(409, f"Port {body.port} is already used by another game")
        if body.port != row["port"]:
            changes["port"] = body.port
            restart_required = ROOMS.is_active(game_id)
    for field in ("password", "server_password"):
        if field in given and getattr(body, field) is not None:
            value = getattr(body, field)
            if value and not valid_password(value):
                raise HTTPException(400, "Passwords cannot contain spaces or control characters, "
                                         "and 'null' and 'none' are reserved by the server")
            changes[field] = value or None
    if "options" in given and body.options is not None:
        current = app_settings.clean_server_options(db.jloads(row["options"], {}))
        try:
            merged = app_settings.clean_server_options(body.options, base=current)
        except app_settings.SettingsError as e:
            raise HTTPException(400, str(e)) from None
        changes["options"] = db.jdumps(merged)
        if any(merged[key] != current[key] for key in RESTART_OPTIONS):
            restart_required = restart_required or ROOMS.is_active(game_id)
    if "visibility" in given and body.visibility is not None:
        if body.visibility not in ("participants", "all"):
            raise HTTPException(400, "Visibility must be 'participants' or 'all'")
        changes["visibility"] = body.visibility
    if "claimable" in given and body.claimable is not None:
        changes["claimable"] = int(body.claimable)
    if "single_claim" in given and body.single_claim is not None:
        changes["single_claim"] = int(body.single_claim)
    if "notes" in given:
        changes["notes"] = (body.notes or "").strip() or None
    if "share_spoiler_when_done" in given and body.share_spoiler_when_done is not None:
        changes["share_spoiler_when_done"] = int(body.share_spoiler_when_done)
    announce_start = False
    if "start_at" in given:
        if body.start_at is None:
            changes["start_at"] = changes["start_stage"] = None
        elif body.start_at != row["start_at"]:
            if not (time.time() + 60 <= body.start_at <= time.time() + 366 * 86400):
                raise HTTPException(400, "Pick a start at least a minute from now and within a year")
            changes["start_at"], changes["start_stage"] = body.start_at, None
            announce_start = True
    if "start_open_minutes" in given and body.start_open_minutes is not None:
        if not 0 <= body.start_open_minutes <= schedule.MAX_OPEN_MINUTES:
            raise HTTPException(400, f"The server can open at most {schedule.MAX_OPEN_MINUTES} minutes before the start")
        changes["start_open_minutes"] = body.start_open_minutes
    if "start_countdown" in given and body.start_countdown is not None:
        changes["start_countdown"] = int(body.start_countdown)
    if "ap_version" in given and body.ap_version is not None:
        try:
            version = versions.check_choice(body.ap_version)
        except versions.VersionError as e:
            raise HTTPException(e.status, e.message) from None
        needed = apbridge.parse_version(row["min_server_version"] or "")
        if needed and (apbridge.version_tuple(version) or needed) < needed:
            raise HTTPException(400, f"This multiworld needs Archipelago {row['min_server_version']} or newer")
        if version != row["ap_version"]:
            changes["ap_version"] = version
            restart_required = restart_required or ROOMS.is_active(game_id)
    if "discord_webhook_url" in given and body.discord_webhook_url is not None:
        webhook = body.discord_webhook_url.strip()
        if webhook and webhook != row["discord_webhook_url"]:
            _check_webhook(webhook, user)
        if (webhook or None) != row["discord_webhook_url"]:
            changes["discord_webhook_url"] = webhook or None
            notify.forget_game(game_id)
    if "spectate_activity" in given and body.spectate_activity is not None:
        changes["spectate_activity"] = int(body.spectate_activity)
    if "remind_days" in given:
        if body.remind_days is not None and not 0 <= body.remind_days <= reminders.MAX_DAYS:
            raise HTTPException(400, f"Remind players after at most {reminders.MAX_DAYS} days")
        changes["remind_days"] = body.remind_days

    if changes:
        assignments = ", ".join(f"{k} = ?" for k in changes)
        db.execute(f"UPDATE games SET {assignments} WHERE id = ?", [*changes.values(), game_id])
        await ROOMS.apply_options(game_id)
    if "finished" in given and body.finished is not None:  # after the spoiler setting, which finishing applies
        history.finish(game_id, body.finished)
    if announce_start:
        schedule.announce(game_id)
    # the log names what really changed: the settings form sends every field
    differs = {key for key, value in changes.items() if key != "start_stage" and (
        db.jloads(row[key], {}) != db.jloads(value, {}) if key == "options" else row[key] != value)}
    if "finished" in given and body.finished is not None and body.finished != (row["finished_at"] is not None):
        differs.add("finished")
    audit.note(request, changed=sorted(differs))
    return {**await _detail(game_id, user, request), "restart_required": restart_required}


@router.delete("/games/{game_id}")
async def remove_game(game_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    await gamestore.delete_game(game_id)
    notify.forget_game(game_id)
    return {"ok": True}


class NotificationTest(BaseModel):
    # The address from the form, so it can be tried before saving. Without one, the saved address is used.
    discord_webhook_url: str | None = Field(default=None, max_length=512)


@router.post("/games/{game_id}/test-notification")
async def test_game_notification(game_id: int, body: NotificationTest, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = managed_game(game_id, user)
    url = (body.discord_webhook_url or "").strip() or row["discord_webhook_url"]
    if not url:
        raise HTTPException(400, "Enter a Discord webhook address first")
    if url != row["discord_webhook_url"]:
        _check_webhook(url, user)
    try:
        await notify.send_test(url, game_id=game_id, game_name=row["name"])
    except notify.DeliveryError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "notify_status": notify.game_status(game_id)}


@router.post("/games/{game_id}/spectate")
async def new_spectate_link(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    """A link for spectators (spectate.py): a new one, which ends the old one if there was one."""
    managed_game(game_id, user)
    spectate.new_link(game_id)
    return await _detail(game_id, user, request)


@router.delete("/games/{game_id}/spectate")
async def end_spectate_link(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    spectate.end_link(game_id)
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# running
# ----------------------------------------------------------------------------------------------------------------------
def _controllable(game_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = get_game(game_id, user)
    if not can_control(row, user):
        raise HTTPException(403, "You are not allowed to control this game")
    return row


@router.post("/games/{game_id}/start")
async def start_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = _controllable(game_id, user)
    if row["start_at"] and row["start_stage"] is None and not manages(row, user):
        raise HTTPException(409, "This game opens at its planned time. An administrator can open it earlier.")
    try:
        await ROOMS.start(game_id, announce=True)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/wake")
async def wake_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    """A player opened the game page: wakes the game up if it is asleep, and keeps it awake a while longer if not."""
    row = get_game(game_id, user)
    if not (manages(row, user) or is_participant(game_id, user["id"])):
        raise HTTPException(403, "Only the players of this game can wake it up")
    try:
        woken = await ROOMS.wake(game_id)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    if woken:  # the log has every start, but not every look at a game that is awake anyway
        audit.record(user, "game.wake", ("game", game_id, row["name"]))
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/stop")
async def stop_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _controllable(game_id, user)
    await ROOMS.stop(game_id)
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/restart")
async def restart_game(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    _controllable(game_id, user)
    try:
        await ROOMS.restart(game_id)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/reset-save")
async def reset_save(game_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    if ROOMS.is_active(game_id):
        raise HTTPException(409, "Stop the server before resetting its progress")
    backups.create(game_id, "before-reset")  # so a reset can be undone
    gamepaths.save_path(game_id).unlink(missing_ok=True)
    ROOMS.forget_progress(game_id)
    history.forget(game_id)
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# backups of the save
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/games/{game_id}/backups")
def list_backups(game_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    return {"backups": backups.listing(game_id), "has_save": gamepaths.save_path(game_id).is_file(),
            "interval_minutes": backups.AUTO_INTERVAL // 60, "keep_auto": backups.KEEP_AUTO}


@router.post("/games/{game_id}/backups")
def create_backup(game_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    entry = backups.create(game_id, "manual")
    if entry is None:
        raise HTTPException(409, "This game has no saved progress yet")
    return list_backups(game_id, user)


@router.post("/games/{game_id}/backups/{name}/restore")
async def restore_backup(game_id: int, name: str, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    if ROOMS.is_active(game_id):
        raise HTTPException(409, "Stop the server before restoring a backup")
    try:
        backups.restore(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    ROOMS.forget_progress(game_id)
    return await _detail(game_id, user, request)


@router.get("/games/{game_id}/backups/{name}")
def download_backup(game_id: int, name: str, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    managed_game(game_id, user)
    try:
        path = backups.path_of(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.delete("/games/{game_id}/backups/{name}")
def delete_backup(game_id: int, name: str, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    try:
        backups.delete(game_id, name)
    except backups.BackupError as e:
        raise HTTPException(404, str(e)) from None
    return list_backups(game_id, user)


class Command(BaseModel):
    command: str = Field(max_length=500)


@router.post("/games/{game_id}/command")
async def send_command(game_id: int, body: Command, request: Request,
                       user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    try:
        await ROOMS.send_command(game_id, body.command)
    except RoomError as e:
        raise HTTPException(409, str(e)) from None
    audit.note(request, command=mask_secrets(body.command.strip()))
    return {"ok": True}


@router.get("/games/{game_id}/log")
def game_log(game_id: int, after: int = 0, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    return ROOMS.log_since(game_id, after)


# ----------------------------------------------------------------------------------------------------------------------
# slots
# ----------------------------------------------------------------------------------------------------------------------
class SlotAssign(BaseModel):
    user_id: int | None


# What a slot forgets when it gets a new player: what the last one said about it, their offer to hand it over and the
# reminders they got; since when the new one has it (the first parameter)
NEW_PLAYER = ("player_since = ?, player_status = NULL, status_note = NULL, status_at = NULL, "
              "handover_at = NULL, handover_to = NULL, handover_note = NULL, reminded_at = NULL, reminders = 0")


def _get_slot(game_id: int, slot: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row:
        raise HTTPException(404, "No such slot")
    return row


@router.get("/games/{game_id}/history")
def progress_history(game_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Every player's checked locations over time, when they reached their goal, and when the game was played."""
    get_game(game_id, user)
    return history.series(game_id)


@router.get("/games/{game_id}/activity")
async def game_activity(game_id: int, after: int = 0, user: sqlite3.Row = Depends(current_user)) -> dict:
    """What happens in the multiworld (activity.py): for everyone who sees the game. On the event loop, where the feed
    grows: a thread could find it changing while it is read."""
    get_game(game_id, user)
    return activity.entries(game_id, after)


class Visit(BaseModel):
    dismiss: bool = False


@router.post("/games/{game_id}/visit")
async def game_visit(game_id: int, body: Visit, user: sqlite3.Row = Depends(current_user)) -> dict:
    """The game page is open: notes the visit, and returns for a player of the game what happened while they were away
    (visits.py). `dismiss` puts that summary away."""
    get_game(game_id, user)
    slots = {r["name"] for r in db.query("SELECT name FROM game_slots WHERE game_id = ? AND user_id = ?",
                                          (game_id, user["id"]))}
    if not slots:
        return {"summary": None}
    since = visits.visit(game_id, user["id"], dismiss=body.dismiss)
    return {"summary": visits.summary(game_id, slots, since) if since else None}


SLOT_STATUSES = ("playing", "stuck", "away")
STATUS_NOTE_LENGTH = 140
STUCK_ANNOUNCE_PAUSE = 10 * 60  # one message per slot in this time, however often someone gets stuck again
_stuck_announced: dict[tuple[int, int], float] = {}


class SlotStatus(BaseModel):
    status: str | None = None  # SLOT_STATUSES, or None for no status
    note: str = Field(default="", max_length=1000)


@router.put("/games/{game_id}/slots/{slot}/status")
async def set_slot_status(game_id: int, slot: int, body: SlotStatus, request: Request,
                          user: sqlite3.Row = Depends(current_user)) -> dict:
    """What the player of a slot says about how they are doing: playing, stuck (waiting for items), or away, with a
    short note. Everyone who sees the game sees it, and the activity feed has it."""
    game = get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if not (manages(game, user) or (target["user_id"] is not None and target["user_id"] == user["id"])):
        raise HTTPException(403, "Only the player of this slot can say how they are doing")
    if target["type"] != 1:
        raise HTTPException(400, "Only player slots have a status")
    if body.status is not None and body.status not in SLOT_STATUSES:
        raise HTTPException(400, "The status must be playing, stuck or away")
    note = "".join(ch for ch in " ".join(body.note.split()) if ch.isprintable()) if body.status else ""
    if len(note) > STATUS_NOTE_LENGTH:
        raise HTTPException(400, f"Keep the note to {STATUS_NOTE_LENGTH} characters")
    if (body.status, note or None) != (target["player_status"], target["status_note"]):
        db.execute("UPDATE game_slots SET player_status = ?, status_note = ?, status_at = ? WHERE game_id = ? AND slot = ?",
                   (body.status, note or None, time.time(), game_id, slot))
        activity.add(game_id, {"kind": "status", "player": target["name"], "status": body.status,
                               **({"note": note} if note else {})})
        if body.status == "stuck" and target["player_status"] != "stuck":
            await _announce_stuck(game, target, note)
    return await _detail(game_id, user, request)


async def _announce_stuck(game: sqlite3.Row, slot: sqlite3.Row, note: str) -> None:
    """Tells Discord that a player is stuck, with where the items hinted for them are, so whoever has one knows."""
    key = (game["id"], slot["slot"])
    if not notify.wanted("player_stuck", game["id"]) or time.time() - _stuck_announced.get(key, 0) < STUCK_ANNOUNCE_PAUSE:
        return
    _stuck_announced[key] = time.time()
    fields = []
    try:
        hints = [h for h in (await ROOMS.tracker(game["id"], slot["slot"]))["hints"] if h["for_me"] and not h["found"]]
    except apbridge.APBridgeError:
        hints = []
    if hints:
        lines = [notify.tr("**{item}** at {location} in {finder}'s world", item=notify.md(h["item"]),
                           location=notify.md(h["location"]), finder=notify.md(h["finder"])) for h in hints[:8]]
        if len(hints) > 8:
            lines.append(notify.tr("…and {n} more", n=len(hints) - 8))
        fields.append((notify.tr("Hinted items they wait for"), "\n".join(lines)))
    owners = {r["user_id"] for r in db.query("SELECT user_id FROM game_slots WHERE game_id = ? AND user_id IS NOT NULL",
                                             (game["id"],))} - {slot["user_id"]}
    notify.emit("player_stuck", notify.tr("{player} is stuck", player=slot["name"]),
                notify.tr("In {game}: {note}", game=notify.md(game["name"]), note=notify.md(note)) if note
                else notify.tr("In {game}, waiting for items.", game=notify.md(game["name"])),
                url=notify.link(f"/games/{game['id']}"), fields=fields, color="warning",
                ping="player_stuck", ping_users=owners, game_id=game["id"])


@router.get("/games/{game_id}/slots/{slot}/tracker")
async def slot_tracker(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """What a slot has received, which of its locations are checked, and its hints. For its owner and admins."""
    game = get_game(game_id, user)
    row = db.query_one("SELECT user_id, type FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row or row["type"] != 1 or (row["user_id"] != user["id"] and not manages(game, user)):
        raise HTTPException(404, "No such slot")
    data = dict(await ROOMS.tracker(game_id, slot))
    # the rules the server ran with as of its save; before the first save, the ones it will be started with
    rules = data.pop("server_rules", None) or app_settings.clean_server_options(db.jloads(game["options"], {}))
    return {**data, **slot_rules(data, rules)}


# ----------------------------------------------------------------------------------------------------------------------
# text client: the manager connects to the server as the player (textclient.py)
# ----------------------------------------------------------------------------------------------------------------------
def _own_slot(game_id: int, slot: int, user: sqlite3.Row) -> None:
    """Only the player of a slot talks in its name; admins have the console."""
    get_game(game_id, user)
    row = db.query_one("SELECT user_id, type FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot))
    if not row or row["type"] != 1 or row["user_id"] != user["id"]:
        raise HTTPException(404, "No such slot")


class ClientPoll(BaseModel):
    after: int = 0


@router.post("/games/{game_id}/slots/{slot}/client")
async def text_client(game_id: int, slot: int, body: ClientPoll, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Keeps the connection open (opening it if needed) and returns the messages after `after`."""
    _own_slot(game_id, slot, user)
    session = TEXT_CLIENTS.session(game_id, slot, user["id"])
    after = body.after if body.after <= session.seq else 0  # a new connection numbers its messages from 1 again
    connected = session.state == "connected"
    return {"state": session.state, "error": session.error, "last": session.seq,
            "messages": [m for m in session.messages if m["seq"] > after],
            "hint_points": session.hint_points if connected else None,
            "hint_cost": session.hint_cost if connected else None}


class ClientSay(BaseModel):
    text: str = Field(max_length=textclient.MAX_MESSAGE_LENGTH * 2)


@router.post("/games/{game_id}/slots/{slot}/client/say")
async def text_client_say(game_id: int, slot: int, body: ClientSay, user: sqlite3.Row = Depends(current_user)) -> dict:
    _own_slot(game_id, slot, user)
    try:
        await TEXT_CLIENTS.say(TEXT_CLIENTS.session(game_id, slot, user["id"]), body.text)
    except textclient.TextClientError as e:
        raise HTTPException(e.status, e.message) from None
    return {"ok": True}


class SlotAction(BaseModel):
    action: str = Field(max_length=20)


# what the player's command is, and the type of the server's message that says it was done
_ACTIONS = {"release": ("!release", "Release"), "collect": ("!collect", "Collect")}


@router.post("/games/{game_id}/slots/{slot}/action")
async def slot_action(game_id: int, slot: int, body: SlotAction, request: Request,
                      user: sqlite3.Row = Depends(current_user)) -> dict:
    """!release or !collect for the player, through their text client connection. The server decides whether its
    rules allow it; its answer comes back."""
    _own_slot(game_id, slot, user)
    if body.action not in _ACTIONS:
        raise HTTPException(400, "Unknown action")
    command, done_type = _ACTIONS[body.action]
    try:
        answer = await TEXT_CLIENTS.command(game_id, slot, user["id"], command, (done_type,))
    except textclient.TextClientError as e:
        raise HTTPException(e.status, e.message) from None
    if answer is None:
        raise HTTPException(504, "The server did not answer in time. The tracker shows in a minute whether it happened.")
    done = answer["type"] == done_type
    audit.note(request, action=body.action, done=done)
    if done:
        await ROOMS.save_soon(game_id)
    return {"done": done, "message": "".join(part["text"] for part in answer["parts"])}


@router.get("/games/{game_id}/slots/{slot}/client/names")
async def text_client_names(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """The slot's items and locations, to suggest them after !hint and !hint_location."""
    _own_slot(game_id, slot, user)
    session = TEXT_CLIENTS.sessions.get((game_id, slot, user["id"]))
    return textclient.suggestions(session) if session else {"items": [], "locations": []}


@router.delete("/games/{game_id}/slots/{slot}/client")
async def text_client_close(game_id: int, slot: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    _own_slot(game_id, slot, user)
    await TEXT_CLIENTS.close((game_id, slot, user["id"]))
    return {"ok": True}


@router.post("/games/{game_id}/slots/{slot}/assign")
async def assign_slot(game_id: int, slot: int, body: SlotAssign, request: Request,
                      user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    target = _get_slot(game_id, slot)
    if target["type"] != 1:
        raise HTTPException(400, "Only player slots can be assigned")
    if body.user_id is not None and not db.query_one("SELECT 1 FROM users WHERE id = ?", (body.user_id,)):
        raise HTTPException(404, "No such user")
    if body.user_id != target["user_id"]:  # what the last player said about the slot is not the new one's
        db.execute(f"UPDATE game_slots SET user_id = ?, {NEW_PLAYER} WHERE game_id = ? AND slot = ?",
                   (body.user_id, time.time(), game_id, slot))
    added = 0
    if body.user_id is not None:
        added = await asyncio.to_thread(gamestore.sync_game_yamls, game_id, slot)
    return {**await _detail(game_id, user, request), "configs_added": added}


@router.post("/games/{game_id}/slots/{slot}/claim")
async def claim_slot(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if not row["claimable"] or target["type"] != 1:
        raise HTTPException(403, "Slots of this game cannot be claimed")
    with db.connection() as conn:  # one locked transaction: two quick clicks cannot both pass the check
        if row["single_claim"] and conn.execute("SELECT 1 FROM game_slots WHERE game_id = ? AND user_id = ?",
                                                (game_id, user["id"])).fetchone():
            raise HTTPException(409, "You already have a slot in this game. Each player may claim only one.")
        claimed = conn.execute(f"UPDATE game_slots SET user_id = ?, {NEW_PLAYER} "
                               "WHERE game_id = ? AND slot = ? AND user_id IS NULL",
                               (user["id"], time.time(), game_id, slot)).rowcount
    if not claimed:
        raise HTTPException(409, "Somebody else already claimed that slot")
    added = await asyncio.to_thread(gamestore.sync_game_yamls, game_id, slot)
    return {**await _detail(game_id, user, request), "configs_added": added}


@router.post("/games/{game_id}/slots/{slot}/unclaim")
async def unclaim_slot(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if target["user_id"] != user["id"]:
        raise HTTPException(403, "That slot is not yours")
    db.execute(f"UPDATE game_slots SET user_id = NULL, {NEW_PLAYER} WHERE game_id = ? AND slot = ?",
               (None, game_id, slot))
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# handing a slot over: its player offers it to someone (or to whoever takes it), who takes it over with its progress
# ----------------------------------------------------------------------------------------------------------------------
class SlotHandover(BaseModel):
    to: str | None = Field(default=None, max_length=100)  # a username; None: whoever takes it
    note: str = Field(default="", max_length=1000)


@router.put("/games/{game_id}/slots/{slot}/handover")
async def set_handover(game_id: int, slot: int, body: SlotHandover, request: Request,
                       user: sqlite3.Row = Depends(current_user)) -> dict:
    """The player of a slot (or an admin) offers it to another user, or to whoever takes it. The slot stays theirs
    until someone takes it over (accept_handover)."""
    game = get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if target["type"] != 1:
        raise HTTPException(400, "Only player slots can be handed over")
    if target["user_id"] is None:
        raise HTTPException(400, "Nobody plays this slot yet")
    if not (manages(game, user) or target["user_id"] == user["id"]):
        raise HTTPException(403, "Only the player of this slot can hand it over")
    note = "".join(ch for ch in " ".join(body.note.split()) if ch.isprintable())
    if len(note) > STATUS_NOTE_LENGTH:
        raise HTTPException(400, f"Keep the note to {STATUS_NOTE_LENGTH} characters")
    to = None
    if body.to and body.to.strip():
        to = db.query_one("SELECT id, username FROM users WHERE username = ? AND disabled = 0", (body.to.strip(),))
        if not to:
            raise HTTPException(404, "There is no user of that name")
        if to["id"] == target["user_id"]:
            raise HTTPException(400, "That user already plays this slot")
        if game["single_claim"] and is_participant(game_id, to["id"]):
            raise HTTPException(409, "That user already has a slot in this game, and each player may have only one")
    db.execute("UPDATE game_slots SET handover_at = ?, handover_to = ?, handover_note = ? WHERE game_id = ? AND slot = ?",
               (time.time(), to["id"] if to else None, note or None, game_id, slot))
    owner = db.query_one("SELECT username FROM users WHERE id = ?", (target["user_id"],))["username"]
    activity.add(game_id, {"kind": "handover", "player": target["name"], "from": owner,
                           **({"to": to["username"]} if to else {}), **({"note": note} if note else {})})
    if to:
        audit.note(request, to=to["username"])
    _announce_handover(game, target, owner, to, note)
    return await _detail(game_id, user, request)


def _announce_handover(game: sqlite3.Row, slot: sqlite3.Row, owner: str, to: sqlite3.Row | None, note: str) -> None:
    fields = [(notify.tr("Note"), notify.md(note))] if note else []
    if to:
        title = notify.tr("{owner} hands {player} over to {user}", owner=owner, player=slot["name"], user=to["username"])
        text = notify.tr("In {game}: {user} can take over the slot ({world}) on its page, with everything found so far.",
                         game=notify.md(game["name"]), user=notify.md(to["username"]), world=notify.md(slot["game"]))
        ping_users: set[int] | None = {to["id"]}
    else:
        title = notify.tr("{player} needs a new player", player=slot["name"])
        text = notify.tr("In {game}, {owner} hands over their slot ({world}). Whoever takes it continues where they "
                         "left off.", game=notify.md(game["name"]), owner=notify.md(owner), world=notify.md(slot["game"]))
        # everyone who can see the game can take it
        ping_users = None if game["visibility"] == "all" else {
            r["user_id"] for r in db.query("SELECT user_id FROM game_slots WHERE game_id = ? AND user_id IS NOT NULL",
                                           (game["id"],))} - {slot["user_id"]}
    notify.emit("slot_handover", title, text, url=notify.link(f"/games/{game['id']}"), fields=fields, color="info",
                ping="slot_handover", ping_users=ping_users, game_id=game["id"])


@router.delete("/games/{game_id}/slots/{slot}/handover")
async def cancel_handover(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    """The player takes their offer back, or the user it was offered to says no."""
    game = get_game(game_id, user)
    target = _get_slot(game_id, slot)
    if not (manages(game, user) or user["id"] in (target["user_id"], target["handover_to"])):
        raise HTTPException(403, "That slot is not yours")
    db.execute("UPDATE game_slots SET handover_at = NULL, handover_to = NULL, handover_note = NULL "
               "WHERE game_id = ? AND slot = ?", (game_id, slot))
    if target["handover_to"] == user["id"] and not manages(game, user) and not is_participant(game_id, user["id"]):
        # whoever said no may no longer see the game
        return {"declined": True}
    return await _detail(game_id, user, request)


@router.post("/games/{game_id}/slots/{slot}/handover/accept")
async def accept_handover(game_id: int, slot: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Takes over a slot offered to this user (or to whoever takes it): its files, its YAML and its progress."""
    game = get_game(game_id, user)
    with db.connection() as conn:  # one locked transaction: two people cannot both take the same offer
        target = conn.execute("SELECT * FROM game_slots WHERE game_id = ? AND slot = ?", (game_id, slot)).fetchone()
        if not target:
            raise HTTPException(404, "No such slot")
        if target["handover_at"] is None or target["user_id"] is None:
            raise HTTPException(409, "This slot is no longer being handed over")
        if target["handover_to"] is not None and target["handover_to"] != user["id"]:
            raise HTTPException(403, "This slot is offered to somebody else")
        if target["user_id"] == user["id"]:
            raise HTTPException(400, "This slot is already yours")
        if game["single_claim"] and conn.execute("SELECT 1 FROM game_slots WHERE game_id = ? AND user_id = ?",
                                                 (game_id, user["id"])).fetchone():
            raise HTTPException(409, "You already have a slot in this game. Each player may have only one.")
        conn.execute(f"UPDATE game_slots SET user_id = ?, {NEW_PLAYER} WHERE game_id = ? AND slot = ?",
                     (user["id"], time.time(), game_id, slot))
        previous = conn.execute("SELECT username FROM users WHERE id = ?", (target["user_id"],)).fetchone()
    previous_name = previous["username"] if previous else None
    # the text client spoke in the last player's name
    await TEXT_CLIENTS.close((game_id, slot, target["user_id"]))
    added = await asyncio.to_thread(gamestore.sync_game_yamls, game_id, slot)
    activity.add(game_id, {"kind": "handover_done", "player": target["name"], "to": user["username"],
                           **({"from": previous_name} if previous_name else {})})
    if previous_name:
        audit.note(request, previous=previous_name)
    notify.emit("slot_handover", notify.tr("{user} takes over {player}", user=user["username"], player=target["name"]),
                notify.tr("In {game}, from {owner}.", game=notify.md(game["name"]), owner=notify.md(previous_name or "?")),
                url=notify.link(f"/games/{game_id}"), color="success", game_id=game_id)
    return {**await _detail(game_id, user, request), "configs_added": added}


# ----------------------------------------------------------------------------------------------------------------------
# files
# ----------------------------------------------------------------------------------------------------------------------
def _get_file(game_id: int, file_id: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM game_files WHERE id = ? AND game_id = ?", (file_id, game_id))
    if not row:
        raise HTTPException(404, "No such file")
    return row


@router.get("/games/{game_id}/files/{file_id}")
def download_file(game_id: int, file_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    game = get_game(game_id, user)
    f = _get_file(game_id, file_id)
    if not file_visible(f, game, user):
        raise HTTPException(404, "No such file")
    path = gamepaths.files_dir(game_id) / f["filename"]
    if not path.is_file():
        raise HTTPException(410, "The file is missing from disk")
    return FileResponse(path, filename=f["filename"], media_type="application/octet-stream")


@router.get("/games/{game_id}/my-files.zip")
def download_my_files(game_id: int, background: BackgroundTasks,
                      user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    game = get_game(game_id, user)
    rows = db.query("SELECT f.* FROM game_files f JOIN game_slots s ON s.game_id = f.game_id AND s.slot = f.slot "
                    "WHERE f.game_id = ? AND f.kind = 'patch' AND s.user_id = ?", (game_id, user["id"]))
    rows = [r for r in rows if (gamepaths.files_dir(game_id) / r["filename"]).is_file()]
    if not rows:
        raise HTTPException(404, "You have no files in this game")
    fd, name = tempfile.mkstemp(suffix=".zip", dir=CONFIG.tmp_dir)
    os.close(fd)  # ZipFile opens it again by name; an open descriptor would keep the file's space until restart
    tmp = Path(name)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for r in rows:
            zf.write(gamepaths.files_dir(game_id) / r["filename"], arcname=r["filename"])
    background.add_task(tmp.unlink, missing_ok=True)
    return FileResponse(tmp, media_type="application/zip", filename=f"{game['name']} - my files.zip".replace("/", "_"))


@router.get("/games/{game_id}/multidata")
def download_multidata(game_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    game = managed_game(game_id, user)
    path = gamepaths.multidata_path(game_id)
    if not path.is_file():
        raise HTTPException(410, "The file is missing from disk")
    return FileResponse(path, filename=f"AP_{game['seed_name'] or game_id}.archipelago",
                        media_type="application/octet-stream")


@router.post("/games/{game_id}/files")
async def upload_file(game_id: int, request: Request, file: UploadFile = File(...), slot: int | None = Form(None),
                      public: bool = Form(False), user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    if slot is not None:
        _get_slot(game_id, slot)
    tmp = CONFIG.tmp_dir / f"upload-{db.now():.6f}-{user['id']}.bin"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        try:
            gamestore.add_file(game_id, file.filename or "file", tmp, slot=slot, public=public)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A file with that name already exists") from None
    finally:
        tmp.unlink(missing_ok=True)
    return await _detail(game_id, user, request)


class FilePatch(BaseModel):
    slot: int | None = None
    public: bool | None = None


@router.patch("/games/{game_id}/files/{file_id}")
async def patch_file(game_id: int, file_id: int, body: FilePatch, request: Request,
                     user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    _get_file(game_id, file_id)
    if "slot" in body.model_fields_set:
        if body.slot is not None:
            _get_slot(game_id, body.slot)
        db.execute("UPDATE game_files SET slot = ? WHERE id = ?", (body.slot, file_id))
    if body.public is not None:
        db.execute("UPDATE game_files SET public = ? WHERE id = ?", (int(body.public), file_id))
    return await _detail(game_id, user, request)


@router.delete("/games/{game_id}/files/{file_id}")
async def delete_file(game_id: int, file_id: int, request: Request, user: sqlite3.Row = Depends(current_user)) -> dict:
    managed_game(game_id, user)
    f = _get_file(game_id, file_id)
    db.execute("DELETE FROM game_files WHERE id = ?", (file_id,))
    (gamepaths.files_dir(game_id) / f["filename"]).unlink(missing_ok=True)
    return await _detail(game_id, user, request)


# ----------------------------------------------------------------------------------------------------------------------
# spectators: a game's link shows it without signing in (spectate.py, watch.html)
# ----------------------------------------------------------------------------------------------------------------------
def _watched(token: str) -> sqlite3.Row:
    game = spectate.game_for(token)
    if game is None:
        raise HTTPException(404, "This link does not show a game (any more)")
    return game


@router.get("/watch/{token}")
async def watch_game(token: str) -> dict:
    """How far every player got, for spectators: no address, password, user names or files."""
    game = _watched(token)
    state = ROOMS.status(game["id"])["state"]
    if state == "stopped" and ROOMS.is_asleep(game):
        state = "sleeping"
    running = state in ("starting", "running")
    progress = await ROOMS.progress(game["id"])
    players = []
    for s in db.query("SELECT * FROM game_slots WHERE game_id = ? AND type = 1 ORDER BY slot", (game["id"],)):
        p = progress.get(s["slot"], {})
        players.append({"slot": s["slot"], "name": s["name"], "game": s["game"], "checked": p.get("checked", 0),
                        "total": s["locations"], "goal_at": history.goal_time(s),
                        "status": status_label(p.get("status", 0), running),
                        # what the player says (stuck, on a break, ...), without their note
                        "player_status": s["player_status"] if s["goal_at"] is None else None})
    series = history.series(game["id"])
    for slot in series["slots"]:
        slot.pop("owner", None)
    return {"name": game["name"], "state": state, "race_mode": bool(game["race_mode"]),
            "start": {"at": game["start_at"], "stage": game["start_stage"] or "planned",
                      "countdown": bool(game["start_countdown"])} if game["start_at"] else None,
            "finished_at": game["finished_at"], "activity": bool(game["spectate_activity"]),
            "players": players, "history": series, "now": time.time()}


@router.get("/watch/{token}/activity")
async def watch_activity(token: str, after: int = 0) -> dict:
    """What happens in the multiworld, if the game shows it to spectators: items, hints, joins and goals."""
    game = _watched(token)
    if not game["spectate_activity"]:
        raise HTTPException(404, "This game does not show its activity to spectators")
    return spectate.feed(game["id"], after)
