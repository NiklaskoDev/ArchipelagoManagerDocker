"""Generation jobs."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import apbridge, audit, db, limits, versions, settings as app_settings
from ..generation import GENERATION, JobRequestError, create_job, job_dir, prepare_players
from ..security import current_user, is_admin, is_organizer

router = APIRouter(prefix="/api")


def job_json(row: sqlite3.Row, *, with_players: bool = True) -> dict:
    options = db.jloads(row["options"], {})
    result = {
        "id": row["id"], "name": row["name"], "status": row["status"], "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"], "game_id": row["game_id"],
        "error": row["error"], "options": options, "host_after": bool(row["host_after"]),
        "test": bool(options.get("test")),
        "created_by": row["created_by"], "creator": row["creator"] if "creator" in row.keys() else None,
        "queue_position": GENERATION.queue_position(row["id"]) if row["status"] == "queued" else None,
    }
    if with_players:
        # config_id is the library config a player file was copied from, to generate with the same configs again
        result["players"] = [
            {"filename": p["filename"], "user_id": p["user_id"], "owner": p["owner"], "config_id": p["config_id"],
             "names": db.jloads(p["player_names"], []), "games": db.jloads(p["games"], []), "error": p["error"]}
            for p in db.query("SELECT p.*, u.username AS owner FROM job_players p "
                              "LEFT JOIN users u ON u.id = p.user_id WHERE p.job_id = ? ORDER BY p.rowid",
                              (row["id"],))]
        meta = job_dir(row["id"]) / "Players" / "meta.yaml"
        result["meta"] = meta.read_text(encoding="utf-8", errors="replace") if options.get("has_meta") and \
            meta.is_file() else None
        signup = db.query_one("SELECT id, name FROM signups WHERE job_id = ? OR test_job_id = ?", (row["id"], row["id"]))
        result["signup"] = {"id": signup["id"], "name": signup["name"]} if signup else None
    return result


def _get_job(job_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT j.*, u.username AS creator FROM jobs j LEFT JOIN users u ON u.id = j.created_by "
                       "WHERE j.id = ?", (job_id,))
    if not row or (row["created_by"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such job")
    return row


class JobCreate(BaseModel):
    config_ids: list[int] = Field(max_length=500)
    name: str | None = Field(default=None, max_length=120)
    spoiler: int = 1
    race: bool = False
    seed: int | None = None
    skip_prog_balancing: bool = False
    plando: list[str] = ["bosses", "connections", "texts"]
    meta: str | None = Field(default=None, max_length=65536)
    host_after: bool = True
    ap_version: str | None = Field(default=None, max_length=20)  # None or "": the image's
    # the new game's server settings instead of the defaults: organizers and administrators only (presets.py)
    server_options: dict | None = None


@router.post("/jobs")
async def start_job(body: JobCreate, request: Request, user: sqlite3.Row = Depends(current_user)):
    admin = is_admin(user)
    if not is_organizer(user) and not app_settings.get_setting("users_can_generate"):
        raise HTTPException(403, "Only administrators can generate games on this server")
    server_options = None
    if body.server_options is not None:
        if not is_organizer(user):
            raise HTTPException(403, "Only organizers and administrators set up the games they generate")
        try:
            server_options = app_settings.clean_server_options(
                body.server_options, base=app_settings.get_setting("default_server_options"))
        except app_settings.SettingsError as e:
            raise HTTPException(400, str(e)) from None
    try:
        version = versions.check_choice(body.ap_version)
        if version:  # its games, to check the players' against
            await apbridge.extra_info(version)
    except versions.VersionError as e:
        raise HTTPException(e.status, e.message) from None
    except apbridge.APBridgeError as e:
        raise HTTPException(503, f"Archipelago {version} is unavailable: {e}") from None
    try:
        async with limits.GENERATING:  # a player's limits (limits.py), counted and taken in one step
            players = prepare_players(body.config_ids, user, is_admin=admin, version=version)
            limits.check_generation(user, sum(len(p.info.docs) for p in players))
            job_id = create_job(user, players, name=body.name, spoiler=body.spoiler, race=body.race, seed=body.seed,
                                skip_prog_balancing=body.skip_prog_balancing, plando=body.plando, meta=body.meta,
                                host_after=body.host_after, ap_version=version, server_options=server_options)
    except JobRequestError as e:
        return JSONResponse(status_code=e.status, content={"detail": e.message, "missing_games": e.missing_games})
    except limits.LimitError as e:
        raise HTTPException(409, e.message) from None
    GENERATION.enqueue(job_id)
    audit.note(request, target=("job", job_id, body.name or None), configs=len(body.config_ids))
    return job_json(_get_job(job_id, user))


@router.get("/jobs")
def list_jobs(user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    base = "SELECT j.*, u.username AS creator FROM jobs j LEFT JOIN users u ON u.id = j.created_by "
    if is_admin(user):
        rows = db.query(base + "ORDER BY j.id DESC LIMIT 100")
    else:
        rows = db.query(base + "WHERE j.created_by = ? ORDER BY j.id DESC LIMIT 100", (user["id"],))
    return [job_json(r, with_players=False) for r in rows]


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    return job_json(_get_job(job_id, user))


@router.get("/jobs/{job_id}/log")
def job_log(job_id: int, after: int = 0, user: sqlite3.Row = Depends(current_user)) -> dict:
    _get_job(job_id, user)
    return GENERATION.log_since(job_id, after)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    _get_job(job_id, user)
    if not await GENERATION.cancel(job_id):
        raise HTTPException(409, "That job is not running or queued")
    return job_json(_get_job(job_id, user))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = _get_job(job_id, user)
    if row["status"] in ("queued", "running"):
        raise HTTPException(409, "Cancel the job before deleting it")
    GENERATION.remove(job_id)
    return {"ok": True}
