"""Player yaml files, stored per user."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import apbridge, configstore, db, yamlform, yamlinfo
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
        "shared": row["shared_at"] is not None,
        "shared_at": row["shared_at"],
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


MAX_CONFIGS_PER_USER = 200


def _store(user_id: int, name: str, data: bytes) -> sqlite3.Row:
    count = db.query_one("SELECT COUNT(*) AS n FROM configs WHERE user_id = ?", (user_id,))["n"]
    if count >= MAX_CONFIGS_PER_USER:
        raise HTTPException(409, f"You already have {MAX_CONFIGS_PER_USER} configs, the most one person can keep. "
                                 "Delete some first.")
    try:
        return configstore.store_config(user_id, name, data)
    except yamlinfo.YamlError as e:
        raise HTTPException(400, str(e)) from None


def get_config(config_id: int, user: sqlite3.Row) -> sqlite3.Row:
    row = db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,))
    if not row or (row["user_id"] != user["id"] and not is_admin(user)):
        raise HTTPException(404, "No such config")
    return row


def readable_config(config_id: int, user: sqlite3.Row) -> sqlite3.Row:
    """A config the user may read and copy: their own, any for an administrator, or one another player shares."""
    row = db.query_one("SELECT c.*, u.username AS owner, u.disabled AS owner_disabled FROM configs c "
                       "JOIN users u ON u.id = c.user_id WHERE c.id = ?", (config_id,))
    if not row or not (row["user_id"] == user["id"] or is_admin(user)
                       or (row["shared_at"] is not None and not row["owner_disabled"])):
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


# ----------------------------------------------------------------------------------------------------------------------
# sharing: players offer their configs to the others, who copy them into their own library
# ----------------------------------------------------------------------------------------------------------------------
def _shared_json(row: sqlite3.Row, user: sqlite3.Row) -> dict:
    return {**config_json(row, owner=row["owner"]), "mine": row["user_id"] == user["id"]}


@router.get("/configs/shared")
def list_shared_configs(user: sqlite3.Row = Depends(current_user)) -> list[dict]:
    rows = db.query("SELECT c.*, u.username AS owner FROM configs c JOIN users u ON u.id = c.user_id "
                    "WHERE c.shared_at IS NOT NULL AND u.disabled = 0 ORDER BY c.shared_at DESC")
    return [_shared_json(r, user) for r in rows]


@router.get("/configs/shared/{config_id}")
def shared_config_detail(config_id: int, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = readable_config(config_id, user)
    return {**_shared_json(row, user), "content": read_config_bytes(row).decode("utf-8-sig")}


class ShareChange(BaseModel):
    shared: bool


@router.put("/configs/{config_id}/shared")
def share_config(config_id: int, body: ShareChange, user: sqlite3.Row = Depends(current_user)) -> dict:
    row = get_config(config_id, user)
    if body.shared and row["user_id"] != user["id"]:
        raise HTTPException(403, "Only its owner can share a config")  # an administrator can stop sharing it
    if body.shared != (row["shared_at"] is not None):
        db.execute("UPDATE configs SET shared_at = ? WHERE id = ?", (db.now() if body.shared else None, config_id))
    return config_json(db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,)))


class ConfigCopy(BaseModel):
    player_name: str | None = Field(default=None, max_length=64)


@router.post("/configs/{config_id}/copy")
def copy_config(config_id: int, body: ConfigCopy, user: sqlite3.Row = Depends(current_user)) -> dict:
    """A copy of a shared config in the user's own library, optionally with their own player name in it."""
    row = readable_config(config_id, user)
    data = read_config_bytes(row)
    if body.player_name and body.player_name.strip():
        try:
            data = yamlinfo.with_player_name(yamlinfo.decode_text(data), body.player_name).encode()
        except yamlinfo.YamlError as e:
            raise HTTPException(400, str(e)) from None
    return config_json(_store(user["id"], row["name"], data))


# Not async: reading yaml takes a while for big files and must not hold up everyone else's requests
@router.post("/configs")
def upload_configs(files: list[UploadFile] = File(...), user: sqlite3.Row = Depends(current_user)) -> dict:
    created, errors = [], []
    for upload in files:
        data = upload.file.read(yamlinfo.MAX_YAML_BYTES + 1)
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
    row = readable_config(config_id, user)
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
    templates, forms = available["templates"], available["forms"]
    return [{"game": game, "source": installed.get(game, {}).get("source"),
             "version": installed.get(game, {}).get("version"), "template": game in templates, "form": game in forms}
            for game in sorted(set(templates) | set(forms), key=str.lower)]


@router.get("/templates/yaml", response_class=PlainTextResponse)
async def get_template(game: str = Query(max_length=200), _user: sqlite3.Row = Depends(current_user)) -> str:
    try:
        text = await apbridge.template_text(game)
    except apbridge.APBridgeError as e:
        raise HTTPException(503, str(e)) from None
    if text is None:
        raise HTTPException(404, f"No template for {game}")
    return text


# ----------------------------------------------------------------------------------------------------------------------
# the options form: a game's options as form fields, and the conversion between a yaml and the form's values
# ----------------------------------------------------------------------------------------------------------------------
async def _form_schema_path(game: str) -> Path:
    try:
        path = await apbridge.options_form_path(game)
    except apbridge.APBridgeError as e:
        raise HTTPException(503, str(e)) from None
    if path is None or not path.is_file():
        raise yamlform.NoFormError(f"There is no form for {game}: it is not installed on this server.")
    return path


@router.get("/templates/options")
async def get_options_form(request: Request, game: str = Query(max_length=200),
                           _user: sqlite3.Row = Depends(current_user)) -> Response:
    try:
        path = await _form_schema_path(game)
    except yamlform.NoFormError as e:
        raise HTTPException(404, str(e)) from None
    data = await asyncio.to_thread(path.read_bytes)
    # Item and location names make a big game's form half a megabyte; as gzip it is a tenth of that.
    if "gzip" in request.headers.get("accept-encoding", ""):
        data = await asyncio.to_thread(gzip.compress, data, 6)
        return Response(data, media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(data, media_type="application/json", headers={"Vary": "Accept-Encoding"})


class YamlText(BaseModel):
    content: str = Field(max_length=yamlinfo.MAX_YAML_BYTES)


@router.post("/configs/form/read")
async def read_form(body: YamlText, _user: sqlite3.Row = Depends(current_user)) -> dict:
    """The form's values for a yaml, or {"unavailable": reason} if it is a file the form cannot edit."""
    try:
        doc, game = await asyncio.to_thread(yamlform.single_player, body.content)
        schema = await asyncio.to_thread(yamlform.load_schema, await _form_schema_path(game))
        return await asyncio.to_thread(yamlform.form_state, doc, game, schema)
    except yamlinfo.YamlError as e:
        raise HTTPException(400, str(e)) from None
    except yamlform.NoFormError as e:
        return {"unavailable": str(e)}


class FormValues(BaseModel):
    content: str | None = Field(default=None, max_length=yamlinfo.MAX_YAML_BYTES)
    game: str = Field(max_length=200)
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/configs/form/write")
async def write_form(body: FormValues, _user: sqlite3.Row = Depends(current_user)) -> dict:
    """The yaml for the form's values, built on `content` (the file being edited) so that what the form does not show
    stays as it is. Nothing is saved: the editor saves the text like any other."""
    try:
        schema = await asyncio.to_thread(yamlform.load_schema, await _form_schema_path(body.game))
        content = await asyncio.to_thread(yamlform.write, body.content, body.game, schema, name=body.name,
                                          description=body.description, values=body.options)
    except (yamlinfo.YamlError, yamlform.FormError, yamlform.NoFormError) as e:
        raise HTTPException(400, str(e)) from None
    return {"content": content}
