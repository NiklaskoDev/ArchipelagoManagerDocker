"""GitHub repositories as sources of custom games. Players propose them; an administrator approves them."""
from __future__ import annotations

import asyncio
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, notify, settings as app_settings, sources
from ..security import admin_user, current_user, is_admin

router = APIRouter(prefix="/api")


def _can_submit(user: sqlite3.Row) -> None:
    if not is_admin(user) and not app_settings.get_setting("users_can_upload_apworlds"):
        raise HTTPException(403, "Only administrators can add games on this server")


def source_json(row: sqlite3.Row) -> dict:
    installed = db.query_one("SELECT status FROM apworlds WHERE id = ?", (row["apworld_id"],)) if row["apworld_id"] else None
    return {
        "id": row["id"], "repo": row["repo"], "repo_url": f"https://github.com/{row['repo']}", "status": row["status"],
        "note": row["note"], "game": row["game"], "asset": row["asset"],
        "added_by": row["added_by"], "adder": row["adder"], "added_at": row["added_at"], "approved_at": row["approved_at"],
        "installed": bool(installed and installed["status"] == "installed"), "installed_tag": row["installed_tag"],
        "latest_tag": row["latest_tag"], "latest_url": row["latest_url"], "checked_at": row["checked_at"],
        "error": row["error"], "update_available": sources.update_available(row),
    }


def _row(source_id: int) -> sqlite3.Row:
    row = db.query_one("SELECT s.*, u.username AS adder FROM apworld_sources s LEFT JOIN users u ON u.id = s.added_by "
                       "WHERE s.id = ?", (source_id,))
    if not row:
        raise HTTPException(404, "No such source")
    return row


def _http(e: sources.SourceError) -> HTTPException:
    return HTTPException(e.status, e.message)


@router.get("/apworld-sources")
def list_sources(user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    rows = db.query("SELECT s.*, u.username AS adder FROM apworld_sources s LEFT JOIN users u ON u.id = s.added_by "
                    "ORDER BY s.status DESC, s.repo COLLATE NOCASE")  # 'pending' sorts before 'approved' this way
    return [source_json(r) for r in rows if r["status"] == "approved" or is_admin(user) or r["added_by"] == user["id"]]


@router.get("/apworld-sources/lookup")
async def lookup(repo: str = Query(max_length=300), user: sqlite3.Row = Depends(current_user)) -> dict:
    """What a repository offers, so the person adding it can see that it is the right one. Downloads nothing."""
    _can_submit(user)
    try:
        release = await asyncio.to_thread(sources.fetch_release, sources.parse_repo(repo))
    except sources.SourceError as e:
        raise _http(e) from None
    existing = db.query_one("SELECT id, status FROM apworld_sources WHERE repo = ?", (release["repo"],))
    return {**{k: v for k, v in release.items() if k != "assets"},
            "assets": [{"name": a["name"], "size": a["size"]} for a in release["assets"]],
            "existing": dict(existing) if existing else None}


class NewSource(BaseModel):
    repo: str = Field(max_length=300)
    asset: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=300)


@router.post("/apworld-sources")
async def add_source(body: NewSource, user: sqlite3.Row = Depends(current_user)) -> dict:
    _can_submit(user)
    admin = is_admin(user)
    try:
        release = await asyncio.to_thread(sources.fetch_release, sources.parse_repo(body.repo))
        asset = sources.pick_asset(release, body.asset)
    except sources.SourceError as e:
        raise _http(e) from None
    if db.query_one("SELECT 1 FROM apworld_sources WHERE repo = ?", (release["repo"],)):
        raise HTTPException(409, f"{release['repo']} was already added")
    now = time.time()
    source_id = db.execute(
        "INSERT INTO apworld_sources(repo, asset, status, note, added_by, added_at, approved_by, approved_at, "
        "latest_tag, latest_url, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (release["repo"], asset["name"], "approved" if admin else "pending", (body.note or "").strip() or None,
         user["id"], now, user["id"] if admin else None, now if admin else None, release["tag"],
         release["release_url"], now))
    if admin:  # an administrator's own choice: install right away, and keep nothing if that fails
        try:
            await sources.install(source_id)
        except sources.SourceError as e:
            db.execute("DELETE FROM apworld_sources WHERE id = ?", (source_id,))
            raise _http(e) from None
    else:
        note = f"\n> {notify.md(body.note.strip())}" if body.note and body.note.strip() else ""
        notify.emit("apworld_pending", f"Custom game waiting for approval: {release['repo']}",
                    f"{notify.md(user['username'])} wants to add [{notify.md(release['repo'])}]"
                    f"(https://github.com/{release['repo']}), release {notify.md(release['tag'])} "
                    f"({notify.md(asset['name'])}). Nothing is downloaded until an administrator approves it.{note}",
                    url=notify.link("/worlds"), color="warning")
    return source_json(_row(source_id))


@router.post("/apworld-sources/{source_id}/approve")
async def approve_source(source_id: int, admin: sqlite3.Row = Depends(admin_user)) -> dict:
    row = _row(source_id)
    if row["status"] != "pending":
        raise HTTPException(409, "That source is already approved")
    db.execute("UPDATE apworld_sources SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
               (admin["id"], time.time(), source_id))
    try:
        await sources.install(source_id)
    except sources.SourceError as e:
        # back to waiting, with the reason, so nothing counts as approved that is not installed
        db.execute("UPDATE apworld_sources SET status = 'pending', approved_by = NULL, approved_at = NULL, error = ? "
                   "WHERE id = ?", (e.message, source_id))
        raise _http(e) from None
    return source_json(_row(source_id))


@router.post("/apworld-sources/{source_id}/update")
async def update_source(source_id: int, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    _row(source_id)
    try:
        await sources.install(source_id)
    except sources.SourceError as e:
        db.execute("UPDATE apworld_sources SET error = ? WHERE id = ?", (e.message, source_id))
        raise _http(e) from None
    return source_json(_row(source_id))


@router.post("/apworld-sources/{source_id}/check")
async def check_source(source_id: int, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    _row(source_id)
    await sources.check(source_id)
    return source_json(_row(source_id))


@router.delete("/apworld-sources/{source_id}")
def delete_source(source_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    """Forgets a source. A game installed from it stays installed; remove that separately."""
    row = _row(source_id)
    if not is_admin(user) and not (row["status"] == "pending" and row["added_by"] == user["id"]):
        raise HTTPException(403, "Only administrators can remove approved sources")
    db.execute("DELETE FROM apworld_sources WHERE id = ?", (source_id,))
    return {"ok": True}
