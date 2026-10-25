"""Copies of the servers' save files, so progress can be brought back after an accident.

While a game's save keeps changing, a copy is taken every half hour and the latest 48 are kept, about a day of play.
A copy is also taken before progress is reset or an older copy is restored, and whenever an admin asks; of those the
latest 20 are kept.
"""
from __future__ import annotations

import calendar
import os
import re
import sqlite3
import time
import zipfile
import zlib
from pathlib import Path

from . import gamepaths
from .config import CONFIG

AUTO_INTERVAL = 30 * 60
KEEP_AUTO = 48
KEEP_OTHER = 20
_NAME = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?-(auto|manual|before-reset|before-restore|before-import)\.apsave$")


class BackupError(Exception):
    pass


def folder(game_id: int) -> Path:
    return gamepaths.game_dir(game_id) / "backups"


def listing(game_id: int) -> list[dict]:
    """The copies of a game's save, newest first. `saved_at` is when the server wrote that state."""
    result = []
    for path in folder(game_id).glob("*.apsave"):
        match = _NAME.match(path.name)
        if not match:
            continue
        stat = path.stat()
        result.append({"name": path.name, "reason": match.group(3), "size": stat.st_size, "saved_at": stat.st_mtime,
                       "created_at": calendar.timegm(time.strptime(match.group(1), "%Y%m%d-%H%M%S"))})
    # the time in the name, then the counter for several in one second
    result.sort(key=lambda b: (b["created_at"], int(_NAME.match(b["name"]).group(2) or 0)), reverse=True)
    return result


def path_of(game_id: int, name: str) -> Path:
    path = folder(game_id) / name
    if not _NAME.match(name) or not path.is_file():
        raise BackupError("No such backup")
    return path


def _read_complete(path: Path) -> bytes | None:
    """The save's bytes, or None while the server is in the middle of writing it (it writes in place)."""
    for _attempt in range(3):
        data = path.read_bytes()
        try:
            zlib.decompress(data)
            return data
        except zlib.error:
            time.sleep(0.5)
    return None


def create(game_id: int, reason: str) -> dict | None:
    """Copies the current save. None if the game has no save yet (or it could not be read whole)."""
    save = gamepaths.save_path(game_id)
    if not save.is_file():
        return None
    saved_at = save.stat().st_mtime
    data = _read_complete(save)
    if data is None:
        return None
    target_dir = folder(game_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    # Several copies in one second are numbered on, so they keep their order (also after old ones were pruned)
    taken = [int(m.group(2) or 0) for path in target_dir.iterdir() if (m := _NAME.match(path.name)) and m.group(1) == stamp]
    name = f"{stamp}-{max(taken) + 1}-{reason}.apsave" if taken else f"{stamp}-{reason}.apsave"
    tmp = target_dir / f".{name}.tmp"
    tmp.write_bytes(data)
    os.utime(tmp, (saved_at, saved_at))
    tmp.replace(target_dir / name)
    _prune(game_id)
    return next(b for b in listing(game_id) if b["name"] == name)


def _prune(game_id: int) -> None:
    entries = listing(game_id)
    autos = [b for b in entries if b["reason"] == "auto"]
    others = [b for b in entries if b["reason"] != "auto"]
    for entry in autos[KEEP_AUTO:] + others[KEEP_OTHER:]:
        (folder(game_id) / entry["name"]).unlink(missing_ok=True)


def maybe_auto(game_id: int) -> dict | None:
    """Takes the half-hourly copy if the save changed since the newest copy and the last automatic one is old enough."""
    save = gamepaths.save_path(game_id)
    if not save.is_file():
        return None
    entries = listing(game_id)
    if entries and max(b["saved_at"] for b in entries) >= save.stat().st_mtime:
        return None  # this state is already kept
    autos = [b for b in entries if b["reason"] == "auto"]
    if autos and time.time() - autos[0]["created_at"] < AUTO_INTERVAL:
        return None
    return create(game_id, "auto")


def restore(game_id: int, name: str) -> None:
    """Puts a copy back as the game's save, after keeping the current one. The server must not be running."""
    data = path_of(game_id, name).read_bytes()  # before the copy below, which may prune old copies
    if gamepaths.save_path(game_id).is_file():
        create(game_id, "before-restore")
    target = gamepaths.save_path(game_id)
    tmp = target.with_name(f".{target.name}.restore")
    tmp.write_bytes(data)
    tmp.replace(target)


def delete(game_id: int, name: str) -> None:
    path_of(game_id, name).unlink()


# ----------------------------------------------------------------------------------------------------------------------
# the backup of everything: the whole data folder as one zip, for an administrator to download
# ----------------------------------------------------------------------------------------------------------------------
# passing files, the templates, which are made again at startup, and the further Archipelago versions, which are large
# and downloaded again from the settings page (versions.py)
_NOT_KEPT = {"tmp", "templates", "ap-versions"}
_DATABASE = {"manager.sqlite3", "manager.sqlite3-wal", "manager.sqlite3-shm", "manager.sqlite3-journal"}


def data_archive(target: Path) -> int:
    """Writes the data folder into a zip at `target` and returns its size. The database goes in as a consistent copy
    (SQLite's backup), not as the file that may be changing; a file that disappears while it is read is left out."""
    data = CONFIG.data_dir
    copy = target.with_suffix(".sqlite3")
    source, dest = sqlite3.connect(CONFIG.db_path), sqlite3.connect(copy)
    try:
        source.backup(dest)
    finally:
        source.close()
        dest.close()
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as zf:
            zf.write(copy, "manager.sqlite3")
            for root, dirs, files in os.walk(data):
                here = Path(root).relative_to(data)
                if here == Path("."):
                    dirs[:] = [d for d in dirs if d not in _NOT_KEPT]
                dirs.sort()
                for name in sorted(files):
                    path = Path(root) / name
                    if (here == Path(".") and name in _DATABASE) or path.is_symlink():
                        continue
                    try:
                        zf.write(path, (here / name).as_posix())
                    except FileNotFoundError:
                        pass
    finally:
        copy.unlink(missing_ok=True)
    return target.stat().st_size
