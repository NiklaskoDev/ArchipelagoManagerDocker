"""Base ROMs: some games need their original ROM on the server to generate. Administrators upload it here.

An upload is checked the way Archipelago checks a file picked in its own settings dialog (the checksums of the right
region and revision) and stored where the game looks for it, under the name it expects. Only places inside the data
folder or Archipelago's own folder for user files (where host.yaml and custom worlds are) are written to: host.yaml could
point a game anywhere on the system.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import apbridge, db
from ..config import CONFIG
from ..security import admin_user

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)


def _rom(game: str, setting: str | None) -> dict:
    for rom in apbridge.roms():
        if rom["game"] == game and rom["setting"] == (setting or None):
            return rom
    raise HTTPException(404, "This game does not ask for a ROM")


def _writable(rom: dict) -> Path:
    """Where the ROM goes, if the manager may write there: inside the data folder or Archipelago's user folder (which
    is in the data folder in the image)."""
    target = Path(rom["path"]).resolve()
    roots = [CONFIG.data_dir, CONFIG.ap_data_home]
    if (apbridge.STATE.where or {}).get("user_path"):
        roots.append(Path(apbridge.STATE.where["user_path"]))
    if not any(target.is_relative_to(root.resolve()) for root in roots):
        raise HTTPException(409, f"host.yaml points {rom['game']} at {rom['path']}, outside the data folder. "
                                 "Put the file there yourself, or point host.yaml into the data folder.")
    return target


@router.get("/roms")
def list_roms(_admin: sqlite3.Row = Depends(admin_user)) -> list[dict]:
    if apbridge.STATE.info is None:
        raise HTTPException(503, "Archipelago is not loaded yet")
    return apbridge.roms()


@router.post("/roms")
async def upload_rom(game: str = Form(...), setting: str = Form(""), file: UploadFile = File(...),
                     user: sqlite3.Row = Depends(admin_user)) -> list[dict]:
    rom = _rom(game, setting)
    target = _writable(rom)
    tmp = CONFIG.tmp_dir / f"rom-{db.now():.6f}-{user['id']}.bin"
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise HTTPException(413, "File is too large")
                out.write(chunk)
        if not size:
            raise HTTPException(400, "The file is empty")
        if rom["checked"]:
            try:
                result = await apbridge.run_helper("check-rom", rom["setting"], str(tmp), timeout=180)
            except apbridge.APBridgeError as e:
                raise HTTPException(502, f"Archipelago could not check the file: {e}") from None
            if not result["valid"]:
                log.info("Refused a ROM for %s: %s", game, result["error"])
                raise HTTPException(400, f"This is not the ROM {game} needs: its checksum is not the one of the "
                                         "region and revision the game expects. Its setup guide says which to use.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp, target)  # a rename where both are on the same disk
    finally:
        tmp.unlink(missing_ok=True)
    apbridge.rom_checked(game, rom["setting"], True if rom["checked"] else None)
    return apbridge.roms()


@router.delete("/roms")
def remove_rom(game: str, setting: str = "", _admin: sqlite3.Row = Depends(admin_user)) -> list[dict]:
    rom = _rom(game, setting)
    target = _writable(rom)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(target)
    apbridge.rom_checked(game, rom["setting"], None)
    return apbridge.roms()
