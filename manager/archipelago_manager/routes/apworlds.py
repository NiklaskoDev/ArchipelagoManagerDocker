"""Custom game support: the library of uploaded .apworld files and the 'game is missing' prompts.

An .apworld is Python code that Archipelago imports, so installing one is an administrator decision. Regular users can
only *submit* a world, which then waits for approval ('pending').
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import apbridge, db, notify, settings as app_settings
from ..config import CONFIG
from ..security import admin_user, current_user, is_admin

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

MAX_APWORLD_UNPACKED = 1024 * 1024 * 1024
_MANIFEST_RE = re.compile(r"^([^/]+)/archipelago\.json$")


class InvalidAPWorld(ValueError):
    pass


def inspect_apworld(path: Path) -> dict:
    """Validates the archive structure and returns {game, module, version, manifest}. Nothing is imported or run."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if sum(i.file_size for i in infos) > MAX_APWORLD_UNPACKED:
                raise InvalidAPWorld("The archive is unreasonably large when unpacked")
            manifests = [m for i in infos if (m := _MANIFEST_RE.match(i.filename))]
            if len(manifests) != 1:
                raise InvalidAPWorld("Not a valid .apworld: expected exactly one '<world>/archipelago.json' manifest")
            module = manifests[0].group(1)
            names = {i.filename for i in infos}
            if f"{module}/__init__.py" not in names and f"{module}/__init__.pyc" not in names:
                raise InvalidAPWorld(f"Not a valid .apworld: '{module}/__init__.py' is missing")
            if zf.getinfo(f"{module}/archipelago.json").file_size > 1024 * 1024:
                raise InvalidAPWorld("The archipelago.json manifest is unreasonably large")
            try:
                manifest = json.loads(zf.read(f"{module}/archipelago.json"))
            except ValueError:
                raise InvalidAPWorld("The archipelago.json manifest is not valid JSON") from None
    except zipfile.BadZipFile:
        raise InvalidAPWorld("The file is not a valid zip archive") from None
    game = manifest.get("game") if isinstance(manifest, dict) else None
    if not isinstance(game, str) or not game.strip():
        raise InvalidAPWorld("The manifest does not name a 'game'")
    if not module.isidentifier() or module.startswith("_"):
        raise InvalidAPWorld(f"'{module}' is not a usable world folder name")
    if (CONFIG.ap_dir / "worlds" / module).exists():
        raise InvalidAPWorld(f"A built-in world named '{module}' already exists, so this cannot be installed")
    version = manifest.get("world_version") or manifest.get("version")
    return {"game": game.strip(), "module": module, "version": str(version) if version else None,
            "manifest": manifest}


def _row_json(row: sqlite3.Row, *, uploader: str | None) -> dict:
    return {
        "id": row["id"], "game": row["game"], "module": row["module"], "original_name": row["original_name"],
        "version": row["version"], "status": row["status"], "size": row["size"],
        "uploaded_by": row["uploaded_by"], "uploader": uploader, "uploaded_at": row["uploaded_at"],
        "load_error": row["load_error"],
        "minimum_ap_version": (db.jloads(row["manifest"], {}) or {}).get("minimum_ap_version"),
    }


async def adopt_orphans() -> int:
    """Registers .apworld files that sit in Archipelago's worlds folder but are unknown to the library.

    They were dropped in by hand or survived a database reset. They load fine, so hiding them from the UI would leave
    worlds nobody can see or remove. Returns how many were adopted.
    """
    where = await apbridge.get_where()
    folder = Path(where["worlds_folder"])
    known = {r["module"] for r in db.query("SELECT module FROM apworlds WHERE status = 'installed'")}
    adopted = 0
    for path in sorted(folder.glob("*.apworld")):
        if not zipfile.is_zipfile(path):
            # Archipelago aborts loading *every* world when one .apworld is not a zip, so move it out of the way
            target = CONFIG.data_dir / "quarantine" / f"{path.name}.{int(db.now())}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(path, target)
            apbridge.STATE.quarantined.append(f"{path.name} (moved to {target})")
            log.warning("%s is not a valid .apworld and would stop Archipelago from loading; moved it to %s", path.name, target)
            continue
        if path.stem in known:
            continue
        try:
            meta = inspect_apworld(path)
        except InvalidAPWorld as e:
            log.warning("Leaving %s alone: %s", path.name, e)
            continue
        if meta["module"] != path.stem:  # Archipelago imports the file as worlds.<file name>, so this cannot work
            log.warning("Leaving %s alone: the world inside is called %s", path.name, meta["module"])
            continue
        apworld_id = db.execute(
            "INSERT INTO apworlds(game, module, original_name, version, status, size, manifest, uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (meta["game"], meta["module"], path.name, meta["version"], "installed", path.stat().st_size,
             db.jdumps(meta["manifest"]), db.now()))
        shutil.copyfile(path, apbridge.apworld_library_path(apworld_id))
        log.info("Adopted %s (%s) found in %s", path.name, meta["game"], folder)
        adopted += 1
    return adopted


async def refresh_load_status() -> None:
    """Reloads the world list from Archipelago and records which installed worlds actually loaded."""
    info = await apbridge.refresh_info()
    if not info:
        return
    loaded = {g["apworld_file"]: g for g in info["games"] if g["apworld_file"]}
    by_name = {g["name"]: g for g in info["games"]}
    failed = info.get("failed", {})
    for row in db.query("SELECT id, game, module FROM apworlds WHERE status = 'installed'"):
        error = None
        if f"{row['module']}.apworld" not in loaded:
            provider = by_name.get(row["game"])
            if row["game"] in failed or row["module"] in failed:
                error = (failed.get(row["game"]) or failed.get(row["module"]) or "").strip().splitlines()[-1][:300]
            elif provider:
                error = f"Not loaded: '{row['game']}' is already provided by another world."
            else:
                error = "Not loaded. It may require a different Archipelago version; check the server log."
        db.execute("UPDATE apworlds SET load_error = ? WHERE id = ?", (error, row["id"]))


async def _install(apworld_id: int) -> list[dict]:
    """Marks a library entry as installed (replacing older versions of the same game) and syncs to Archipelago."""
    row = db.query_one("SELECT * FROM apworlds WHERE id = ?", (apworld_id,))
    replaced = db.query("SELECT * FROM apworlds WHERE status = 'installed' AND id != ? AND (game = ? OR module = ?)",
                        (apworld_id, row["game"], row["module"]))
    for old in replaced:
        db.execute("DELETE FROM apworlds WHERE id = ?", (old["id"],))
        apbridge.apworld_library_path(old["id"]).unlink(missing_ok=True)
        if old["module"] != row["module"]:
            await apbridge.remove_apworld_file(old["module"])
    db.execute("UPDATE apworlds SET status = 'installed' WHERE id = ?", (apworld_id,))
    await apbridge.sync_apworlds()
    await refresh_load_status()
    return [{"id": r["id"], "version": r["version"]} for r in replaced]


@router.get("/worlds")
def list_worlds(_user: sqlite3.Row = Depends(current_user)) -> dict:
    """Every game this server can generate for."""
    games = apbridge.installed_games()
    return {
        "ap_version": apbridge.ap_version(),
        "available": apbridge.STATE.info is not None,
        "error": apbridge.STATE.error,
        "games": sorted(games.values(), key=lambda g: g["name"].lower()),
    }


@router.get("/apworlds")
def list_apworlds(user: sqlite3.Row = Depends(current_user)) -> dict:
    rows = db.query("SELECT a.*, u.username AS uploader FROM apworlds a LEFT JOIN users u ON u.id = a.uploaded_by "
                    "ORDER BY a.game COLLATE NOCASE, a.uploaded_at DESC")
    visible = [r for r in rows if r["status"] == "installed" or is_admin(user) or r["uploaded_by"] == user["id"]]
    return {
        "items": [_row_json(r, uploader=r["uploader"]) for r in visible],
        "can_install": is_admin(user),
        "can_submit": is_admin(user) or app_settings.get_setting("users_can_upload_apworlds"),
        "ap_version": apbridge.ap_version(),
    }


async def add_to_library(path: Path, filename: str, *, uploaded_by: int | None, install: bool) -> dict:
    """Checks an .apworld and adds it to the library: installed right away, or waiting for approval.

    Moves the file. Raises InvalidAPWorld with a reason fit for the user.
    """
    meta = inspect_apworld(path)
    version_tuple = apbridge.version_tuple()
    minimum = str(meta["manifest"].get("minimum_ap_version") or "")
    if version_tuple and re.fullmatch(r"\d+\.\d+\.\d+", minimum):
        if tuple(int(p) for p in minimum.split(".")) > version_tuple:
            raise InvalidAPWorld(f"This world needs Archipelago {minimum} or newer, "
                                 f"but this server runs {apbridge.ap_version()}")
    apworld_id = db.execute(
        "INSERT INTO apworlds(game, module, original_name, version, status, size, manifest, uploaded_by, "
        "uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (meta["game"], meta["module"], filename, meta["version"], "pending", path.stat().st_size,
         db.jdumps(meta["manifest"]), uploaded_by, db.now()))
    shutil.move(path, apbridge.apworld_library_path(apworld_id))
    replaced = await _install(apworld_id) if install else []
    row = db.query_one("SELECT a.*, u.username AS uploader FROM apworlds a LEFT JOIN users u ON u.id = a.uploaded_by "
                       "WHERE a.id = ?", (apworld_id,))
    return {**_row_json(row, uploader=row["uploader"]), "replaced": replaced}


@router.post("/apworlds")
async def upload_apworld(file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user)) -> dict:
    if not is_admin(user) and not app_settings.get_setting("users_can_upload_apworlds"):
        raise HTTPException(403, "Only administrators can add games on this server")
    filename = Path(file.filename or "world.apworld").name
    if not filename.lower().endswith(".apworld"):
        raise HTTPException(400, "Please upload an .apworld file")
    tmp = CONFIG.tmp_dir / f"upload-{db.now():.6f}-{user['id']}.apworld"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        try:
            entry = await add_to_library(tmp, filename, uploaded_by=user["id"], install=is_admin(user))
        except InvalidAPWorld as e:
            raise HTTPException(400, str(e)) from None
    finally:
        tmp.unlink(missing_ok=True)
    if entry["status"] == "pending":
        version = f" {entry['version']}" if entry["version"] else ""
        notify.emit("apworld_pending", f"Custom game waiting for approval: {entry['game']}",
                    f"{notify.md(user['username'])} submitted **{notify.md(entry['game'])}**{notify.md(version)} "
                    f"({notify.md(filename)}). An administrator has to approve it before it can be used.",
                    url=notify.link("/worlds"), color="warning")
    return entry


def _get(apworld_id: int) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM apworlds WHERE id = ?", (apworld_id,))
    if not row:
        raise HTTPException(404, "No such APWorld")
    return row


@router.post("/apworlds/{apworld_id}/approve")
async def approve_apworld(apworld_id: int, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    row = _get(apworld_id)
    if row["status"] != "pending":
        raise HTTPException(409, "That APWorld is already installed")
    replaced = await _install(apworld_id)
    row = db.query_one("SELECT a.*, u.username AS uploader FROM apworlds a LEFT JOIN users u ON u.id = a.uploaded_by "
                       "WHERE a.id = ?", (apworld_id,))
    return {**_row_json(row, uploader=row["uploader"]), "replaced": replaced}


@router.delete("/apworlds/{apworld_id}")
async def delete_apworld(apworld_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = _get(apworld_id)
    own_pending = row["status"] == "pending" and row["uploaded_by"] == user["id"]
    if not is_admin(user) and not own_pending:
        raise HTTPException(403, "Only administrators can remove installed games")
    db.execute("DELETE FROM apworlds WHERE id = ?", (apworld_id,))
    apbridge.apworld_library_path(apworld_id).unlink(missing_ok=True)
    if row["status"] == "installed":
        await apbridge.remove_apworld_file(row["module"])
        await refresh_load_status()
    return {"ok": True}


@router.get("/apworlds/{apworld_id}/download")
def download_apworld(apworld_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    row = _get(apworld_id)
    if row["status"] != "installed" and not is_admin(user) and row["uploaded_by"] != user["id"]:
        raise HTTPException(404, "No such APWorld")
    path = apbridge.apworld_library_path(apworld_id)
    if not path.is_file():
        raise HTTPException(410, "The file is missing from disk")
    return FileResponse(path, media_type="application/zip", filename=f"{row['module']}.apworld")


@router.get("/missing-games")
def missing_games(user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    """Games used by the caller's configs and visible hosted games that this server does not have installed."""
    if apbridge.STATE.info is None:
        return []
    found: dict[str, dict] = {}

    def entry(game: str) -> dict:
        return found.setdefault(game, {"game": game, "configs": [], "games": [], "pending": []})

    if is_admin(user):
        configs = db.query("SELECT c.id, c.name, c.games, u.username AS owner FROM configs c "
                           "JOIN users u ON u.id = c.user_id")
        games = db.query("SELECT id, name, games FROM games")
    else:
        configs = db.query("SELECT c.id, c.name, c.games, u.username AS owner FROM configs c "
                           "JOIN users u ON u.id = c.user_id WHERE c.user_id = ?", (user["id"],))
        games = db.query("SELECT g.id, g.name, g.games FROM games g WHERE g.visibility = 'all' OR EXISTS "
                         "(SELECT 1 FROM game_slots s WHERE s.game_id = g.id AND s.user_id = ?)", (user["id"],))
    for row in configs:
        for game in apbridge.missing_games(db.jloads(row["games"], [])):
            entry(game)["configs"].append({"id": row["id"], "name": row["name"], "owner": row["owner"]})
    for row in games:
        for game in apbridge.missing_games(db.jloads(row["games"], [])):
            entry(game)["games"].append({"id": row["id"], "name": row["name"]})
    for item in found.values():
        for p in db.query("SELECT a.id, a.uploaded_by, u.username AS uploader FROM apworlds a "
                          "LEFT JOIN users u ON u.id = a.uploaded_by WHERE a.status = 'pending' AND a.game = ?",
                          (item["game"],)):
            if is_admin(user) or p["uploaded_by"] == user["id"]:
                item["pending"].append({"id": p["id"], "uploader": p["uploader"]})
            else:
                item["pending"].append({"id": None, "uploader": None})  # someone already submitted one
    return sorted(found.values(), key=lambda i: i["game"].lower())
