"""Further Archipelago versions (versions.py): what is installed, installing one, and removing one."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import audit, db, versions
from ..security import admin_user, current_user, is_admin

router = APIRouter(prefix="/api")


@router.get("/ap-versions")
def list_versions(user: sqlite3.Row = Depends(current_user)) -> dict:
    """Administrators get everything; players, who may choose one to generate with, the versions that can be used."""
    listing = versions.listing()
    if is_admin(user):
        return listing
    return {"default": listing["default"],
            "versions": [{"version": v["version"], "status": v["status"]} for v in listing["versions"]
                         if v["status"] == "installed"]}


class VersionInstall(BaseModel):
    version: str = Field(max_length=20)


@router.post("/ap-versions")
async def install_version(body: VersionInstall, request: Request, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    try:
        versions.start_install(body.version)
    except versions.VersionError as e:
        raise HTTPException(e.status, e.message) from None
    audit.note(request, version=body.version.strip().lstrip("v"))
    return versions.listing()


@router.get("/ap-versions/{version}/log", response_class=PlainTextResponse)
def version_log(version: str, _admin: sqlite3.Row = Depends(admin_user)) -> str:
    if not versions.VERSION.match(version) or not db.query_one("SELECT 1 FROM ap_versions WHERE version = ?",
                                                                 (version,)):
        raise HTTPException(404, "No such version")
    return versions.log_tail(version)


@router.delete("/ap-versions/{version}")
async def remove_version(version: str, _admin: sqlite3.Row = Depends(admin_user)) -> dict:
    try:
        await versions.remove(version)
    except versions.VersionError as e:
        raise HTTPException(e.status, e.message) from None
    return versions.listing()
