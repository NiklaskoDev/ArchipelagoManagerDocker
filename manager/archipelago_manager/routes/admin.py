"""Server-wide settings, system information and the Archipelago host.yaml."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from .. import __version__, apbridge, audit, backups, db, discord_auth, notify, settings as app_settings, updates
from ..config import CONFIG
from ..rooms import ROOMS
from ..security import admin_user
from .apworlds import refresh_load_status

router = APIRouter(prefix="/api")


def _settings_json(values: dict) -> dict:
    return {**app_settings.public_settings(values), "port_range": [CONFIG.port_start, CONFIG.port_end],
            "public_host_from_env": CONFIG.public_host, "notify_status": notify.status(),
            "discord_redirect_uri": (values["site_url"] + discord_auth.CALLBACK_PATH) if values["site_url"] else None}


@router.get("/settings")
def read_settings(_admin: sqlite3.Row = Depends(admin_user)) -> dict:
    return _settings_json(app_settings.get_settings())


class SettingsPatch(BaseModel):
    public_host: str | None = None
    allow_registration: bool | None = None
    users_can_start_rooms: bool | None = None
    users_can_generate: bool | None = None
    users_can_upload_apworlds: bool | None = None
    auto_restart_crashed: bool | None = None
    default_server_options: dict | None = None
    discord_webhook_url: str | None = Field(default=None, max_length=512)
    notify_events: list[str] | None = None
    notify_language: str | None = None
    notify_mentions: bool | None = None
    site_url: str | None = Field(default=None, max_length=512)
    discord_login: bool | None = None
    discord_client_id: str | None = Field(default=None, max_length=64)
    discord_client_secret: str | None = Field(default=None, max_length=200)
    discord_signup: bool | None = None
    discord_guild_id: str | None = Field(default=None, max_length=64)


@router.patch("/settings")
def write_settings(body: SettingsPatch, request: Request, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    before = app_settings.get_settings()
    try:
        updated = app_settings.update_settings(body.model_dump(exclude_unset=True, exclude_none=True))
    except app_settings.SettingsError as e:
        raise HTTPException(400, str(e)) from None
    audit.note(request, changed=sorted(key for key in updated if updated[key] != before.get(key)))
    return _settings_json(updated)


class NotificationTest(BaseModel):
    # The address from the form, so it can be tried before saving. Without one, the saved address is used.
    discord_webhook_url: str | None = Field(default=None, max_length=512)


@router.post("/settings/test-notification")
async def test_notification(body: NotificationTest, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    url = (body.discord_webhook_url or "").strip() or app_settings.get_setting("discord_webhook_url")
    if not url:
        raise HTTPException(400, "Enter a Discord webhook address first")
    if not app_settings.valid_url(url):
        raise HTTPException(400, "The webhook address must start with http:// or https://")
    try:
        await notify.send_test(url)
    except notify.DeliveryError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "notify_status": notify.status()}


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


@router.get("/system")
async def system_info(_admin: sqlite3.Row = Depends(admin_user)) -> dict:
    usage = shutil.disk_usage(CONFIG.data_dir)
    running = sum(1 for r in db.query("SELECT id FROM games") if ROOMS.is_active(r["id"]))
    where = apbridge.STATE.where or {}
    return {
        "manager_version": __version__,
        "ap_version": apbridge.ap_version(),
        "ap_error": apbridge.STATE.error,
        "ap_dir": str(CONFIG.ap_dir),
        "worlds_folder": where.get("worlds_folder"),
        "host_yaml": where.get("host_yaml"),
        "world_count": len(apbridge.installed_games(include_hidden=True)),
        "failed_worlds": (apbridge.STATE.info or {}).get("failed", {}),
        "quarantined": apbridge.STATE.quarantined,
        "update": updates.status(),
        "data_dir": str(CONFIG.data_dir),
        "disk": {"total": usage.total, "free": usage.free},
        "data_bytes": await asyncio.to_thread(_dir_size, CONFIG.data_dir),
        "port_range": [CONFIG.port_start, CONFIG.port_end],
        "games": db.query_one("SELECT COUNT(*) AS n FROM games")["n"],
        "running_games": running,
        "users": db.query_one("SELECT COUNT(*) AS n FROM users")["n"],
    }


@router.post("/system/check-updates")
async def check_updates(_admin: sqlite3.Row = Depends(admin_user)) -> dict:
    if not CONFIG.update_check:
        raise HTTPException(409, "Update checks are turned off (UPDATE_CHECK=false)")
    await updates.check_now()
    return updates.status()


@router.post("/system/reload-worlds")
async def reload_worlds(_admin: sqlite3.Row = Depends(admin_user)) -> dict:
    await apbridge.sync_apworlds()
    await refresh_load_status()
    if apbridge.STATE.info is None:
        raise HTTPException(503, apbridge.STATE.error or "Archipelago is unavailable")
    return {"world_count": len(apbridge.installed_games(include_hidden=True)),
            "failed_worlds": apbridge.STATE.info.get("failed", {})}


# ----------------------------------------------------------------------------------------------------------------------
# host.yaml
# ----------------------------------------------------------------------------------------------------------------------
async def _host_yaml_path() -> Path:
    try:
        result = await apbridge.run_helper("host-yaml", timeout=60)
    except apbridge.APBridgeError as e:
        raise HTTPException(503, str(e)) from None
    return Path(result["path"])


@router.get("/host-yaml")
async def read_host_yaml(_admin: sqlite3.Row = Depends(admin_user)) -> dict:
    path = await _host_yaml_path()
    # Archipelago keeps host.yaml inside its install folder when that is writable, which only happens when running as
    # root. Inside a container that is lost with the container, so the UI warns about it.
    persistent = CONFIG.data_dir in path.resolve().parents
    return {"path": str(path), "content": path.read_text(encoding="utf-8"), "persistent": persistent}


class HostYaml(BaseModel):
    content: str = Field(max_length=512 * 1024)


@router.put("/host-yaml")
async def write_host_yaml(body: HostYaml, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    path = await _host_yaml_path()
    try:
        yaml.safe_load(body.content)
    except yaml.MarkedYAMLError as e:
        line = f" (line {e.problem_mark.line + 1})" if e.problem_mark else ""
        raise HTTPException(400, f"Invalid YAML{line}: {e.problem}") from None
    candidate = CONFIG.tmp_dir / "host-candidate.yaml"
    candidate.write_text(body.content, encoding="utf-8")
    try:
        await apbridge.run_helper("validate-host-yaml", str(candidate), timeout=60)
    except apbridge.APBridgeError as e:
        raise HTTPException(400, f"Archipelago rejected this file: {e}") from None
    finally:
        candidate.unlink(missing_ok=True)
    shutil.copyfile(path, path.with_suffix(".yaml.bak"))
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path)}


# ----------------------------------------------------------------------------------------------------------------------
# audit log and the backup of everything
# ----------------------------------------------------------------------------------------------------------------------
@router.get("/audit")
def audit_log(before: int | None = None, user_id: int | None = None, action: str | None = None,
              _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    """Who changed what, newest first, 100 at a time (`before`: the oldest id seen so far)."""
    entries = audit.entries(before=before, user_id=user_id, action=action, limit=101)
    return {"entries": entries[:100], "more": len(entries) > 100}


# Made archives wait here for their download: a POST makes one (a cross-site link cannot), the GET fetches it once
_ARCHIVES: dict[str, tuple[Path, float]] = {}
ARCHIVE_MINUTES = 15


@router.post("/system/backup")
async def make_backup(request: Request, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    now = time.time()
    for token, (path, expires) in list(_ARCHIVES.items()):
        if expires < now:
            path.unlink(missing_ok=True)
            del _ARCHIVES[token]
    token = secrets.token_urlsafe(24)
    path = CONFIG.tmp_dir / f"backup-{token}.zip"
    try:
        size = await asyncio.to_thread(backups.data_archive, path)
    except OSError as e:
        path.unlink(missing_ok=True)
        raise HTTPException(507, f"The backup could not be written: {e.strerror or e}") from None
    _ARCHIVES[token] = (path, now + ARCHIVE_MINUTES * 60)
    audit.note(request, size=size)
    return {"url": f"/api/system/backup/{token}", "size": size}


@router.get("/system/backup/{token}")
def download_backup(token: str, _admin: sqlite3.Row = Depends(admin_user)) -> FileResponse:
    path, _expires = _ARCHIVES.pop(token, (None, 0))
    if path is None or not path.is_file():
        raise HTTPException(404, "This backup is no longer there. Make a new one.")
    name = f"archipelago-manager-{time.strftime('%Y-%m-%d-%H%M')}.zip"
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(path.unlink, missing_ok=True))
