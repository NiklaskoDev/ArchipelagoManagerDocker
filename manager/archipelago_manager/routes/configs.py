"""Player yaml files, stored per user."""
from __future__ import annotations

import hashlib
import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import apbridge, configstore, db, yamlinfo
from ..configstore import config_path
from ..security import current_user, is_admin

router = APIRouter(prefix="/api")


def config_json(row: sqlite3.Row, *, owner: str | None = None) -> dict:
    games = db.jloads(row["games"], [])
    result = {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "games": games,
        "player_names": db.jloads(row["player_names"], []),
        "description": row["description"],
        "size": row["size"],
        "source_game_id": row["source_game_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "missing_games": apbridge.missing_games(games),
        "missing_roms": apbridge.missing_files(games),
    }
    if owner is not None:
        result["owner"] = owner
    return result


def _analyze(data: bytes, name: str):
    try:
        return configstore.analyze_bytes(data, name)
    except yamlinfo.YamlError as e:
        raise HTTPException(400, str(e)) from None


def _store(user_id: int, name: str, data: bytes) -> sqlite3.Row:
    try:
        return configstore.store_config(user_id, name, data)
    except yamlinfo.YamlError as e:
        raise HTTPException(400, str(e)) from None


def get_config(config_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,))
    if not row or (row["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such config")
    return row


def read_config_bytes(row: sqlite3.Row) -> bytes:
    try:
        return config_path(row["user_id"], row["id"]).read_bytes()
    except FileNotFoundError:
        raise HTTPException(410, "The file for this config is missing from disk") from None


@router.get("/configs")
def list_configs(all: bool = False, user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    base = ("SELECT c.*, u.username AS owner FROM configs c JOIN users u ON u.id = c.user_id ")
    if all and is_admin(user):
        rows = db.query(base + "ORDER BY u.username COLLATE NOCASE, c.name COLLATE NOCASE")
    else:
        rows = db.query(base + "WHERE c.user_id = ? ORDER BY c.name COLLATE NOCASE", (user["id"],))
    return [config_json(r, owner=r["owner"]) for r in rows]


@router.post("/configs")
async def upload_configs(files: list[UploadFile] = File(...), user: sqlite3.Row = Depends(current_user)) -> dict:
    created, errors = [], []
    for upload in files:
        data = await upload.read(yamlinfo.MAX_YAML_BYTES + 1)
        try:
            created.append(config_json(_store(user["id"], upload.filename or "config.yaml", data)))
        except HTTPException as e:
            errors.append({"filename": upload.filename, "error": e.detail})
    if not created and errors:
        raise HTTPException(400, "; ".join(f"{e['filename']}: {e['error']}" for e in errors))
    return {"created": created, "errors": errors}


class NewConfig(BaseModel):
    name: str = Field(max_length=120)
    content: str = Field(max_length=yamlinfo.MAX_YAML_BYTES)


@router.post("/configs/text")
def create_config(body: NewConfig, user: sqlite3.Row = Depends(current_user)) -> dict:
    return config_json(_store(user["id"], body.name, body.content.encode()))


@router.get("/configs/{config_id}")
def get_config_detail(config_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_config(config_id, user)
    data = read_config_bytes(row)
    _text, info = _analyze(data, row["name"])
    owner = db.query_one("SELECT username FROM users WHERE id = ?", (row["user_id"],))
    return {**config_json(row, owner=owner["username"] if owner else None),
            "content": data.decode("utf-8-sig"), "warnings": info.warnings}


class ConfigUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    content: str | None = Field(default=None, max_length=yamlinfo.MAX_YAML_BYTES)


@router.put("/configs/{config_id}")
def update_config(config_id: int, body: ConfigUpdate, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_config(config_id, user)
    name = row["name"]
    if body.name is not None:
        name = configstore.unique_name(row["user_id"], configstore.normalize_name(body.name), ignore_id=config_id)
    data = body.content.encode() if body.content is not None else read_config_bytes(row)
    _text, info = _analyze(data, name)
    db.execute("UPDATE configs SET name = ?, player_names = ?, games = ?, description = ?, sha256 = ?, size = ?, "
               "updated_at = ? WHERE id = ?",
               (name, db.jdumps(info.player_names), db.jdumps(info.games), info.description,
                hashlib.sha256(data).hexdigest(), len(data), db.now(), config_id))
    if body.content is not None:
        configstore.write_atomic(config_path(row["user_id"], config_id), data)
    return {**config_json(db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,))),
            "warnings": info.warnings}


@router.delete("/configs/{config_id}")
def delete_config(config_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_config(config_id, user)
    db.execute("DELETE FROM configs WHERE id = ?", (config_id,))
    config_path(row["user_id"], config_id).unlink(missing_ok=True)
    return {"ok": True}


@router.get("/configs/{config_id}/download")
def download_config(config_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    row = get_config(config_id, user)
    path = config_path(row["user_id"], config_id)
    if not path.is_file():
        raise HTTPException(410, "The file for this config is missing from disk")
    return FileResponse(path, media_type="application/x-yaml", filename=row["name"])


# ----------------------------------------------------------------------------------------------------------------------
# templates
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/templates")
async def list_templates(_user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    try:
        available = await apbridge.ensure_templates()
    except apbridge.APBridgeError as e:
        raise HTTPException(503, str(e)) from None
    installed = apbridge.installed_games()
    return [{"game": game, "source": installed.get(game, {}).get("source"),
             "version": installed.get(game, {}).get("version")}
            for game in sorted(available, key=str.lower)]


@router.get("/templates/yaml", response_class=PlainTextResponse)
async def get_template(game: str = Query(max_length=200), _user: sqlite3.Row = Depends(current_user)) -> str:
    try:
        text = await apbridge.template_text(game)
    except apbridge.APBridgeError as e:
        raise HTTPException(503, str(e)) from None
    if text is None:
        raise HTTPException(404, f"No template for {game}")
    return text
