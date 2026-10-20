"""Importing multiworld archives, linking their slots to users, and removing games again."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import apbridge, configstore, db, gamepaths, settings as app_settings, yamlinfo
from .config import CONFIG
from .rooms import ROOMS, allocate_port

log = logging.getLogger(__name__)

MAX_ARCHIVE_MEMBERS = 5000
_PATCH_RE = re.compile(r"^AP_[^_]+_P(\d+)_")
_PATCH_FALLBACK_RE = re.compile(r"_P(\d+)_")


class GameImportError(ValueError):
    """The upload is not something we can host. The message is meant for the user."""


@dataclass
class StoredFile:
    filename: str
    kind: str
    slot: int | None
    size: int


def classify(name: str) -> tuple[str, int | None]:
    lower = name.lower()
    if lower.endswith(".archipelago"):
        return "multidata", None
    if lower.endswith((".yaml", ".yml")):
        return "yaml", None
    if lower.endswith(".txt") and "spoiler" in lower:
        return "spoiler", None
    match = _PATCH_RE.match(name) or _PATCH_FALLBACK_RE.search(name)
    if match:
        return "patch", int(match.group(1))
    return "other", None


def _unique_filename(directory: Path, filename: str) -> str:
    candidate, stem, suffix, counter = filename, Path(filename).stem, Path(filename).suffix, 2
    while (directory / candidate).exists():
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def extract_archive(src: Path, work: Path, max_total: int) -> list[StoredFile]:
    """Unpacks a generation result into work/multidata.archipelago and work/files/. Blocking."""
    files_dir = work / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    stored: list[StoredFile] = []
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise GameImportError("The file is not a valid zip archive") from None
    with zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise GameImportError(f"The archive contains too many files ({len(members)})")
        if sum(i.file_size for i in members) > max_total:
            raise GameImportError("The archive is too large when unpacked")
        have_multidata = False
        for info in members:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            base = parts[-1] if parts else ""
            if not base or base.startswith(".") or "__MACOSX" in parts:
                continue
            kind, slot = classify(base)
            if kind == "multidata" and not have_multidata:
                have_multidata = True
                with zf.open(info) as source, open(work / "multidata.archipelago", "wb") as target:
                    shutil.copyfileobj(source, target)
                continue
            if kind == "multidata":
                kind = "other"  # a second .archipelago file: keep it, but it is not what we host
            filename = _unique_filename(files_dir, yamlinfo.safe_filename(base))
            with zf.open(info) as source, open(files_dir / filename, "wb") as target:
                shutil.copyfileobj(source, target)
            stored.append(StoredFile(filename, kind, slot, (files_dir / filename).stat().st_size))
    return stored


def _version_text(parts: list[int]) -> str:
    return ".".join(str(p) for p in parts)


async def import_game(src: Path, original_name: str, *, name: str | None = None, created_by: int | None = None,
                      source: str = "upload", job_id: int | None = None,
                      owners: dict[str, int] | None = None) -> int:
    """Turns an uploaded zip or .archipelago file into a hosted-ready game. Returns the new game id.

    `owners` maps lower-cased slot names to user ids. Without it, slots are matched against everybody's configs.
    """
    work = CONFIG.tmp_dir / f"import-{uuid.uuid4().hex}"
    try:
        work.mkdir(parents=True)
        if zipfile.is_zipfile(src):
            stored = await asyncio.to_thread(extract_archive, src, work, CONFIG.max_extract_bytes)
        else:
            (work / "files").mkdir()
            shutil.copyfile(src, work / "multidata.archipelago")
            stored = []
        multidata = work / "multidata.archipelago"
        if not multidata.is_file():
            raise GameImportError("No .archipelago multiworld file found. Upload the zip created by the generator "
                                  "or the .archipelago file itself.")
        try:
            meta = await apbridge.run_helper("multidata", str(multidata), timeout=120)
        except apbridge.APBridgeError as e:
            raise GameImportError(f"Could not read the multiworld data: {e}") from None

        current = apbridge.version_tuple()
        needed = tuple(meta["minimum_server_version"])
        if current and needed and needed > current:
            raise GameImportError(f"This multiworld needs Archipelago {_version_text(list(needed))} or newer, "
                                  f"but this server runs {apbridge.ap_version()}.")

        stem = Path(original_name).stem
        display = (name or "").strip() or (stem if stem and not stem.startswith("AP_") else f"Seed {meta['seed_name']}")
        options = app_settings.get_setting("default_server_options")

        for attempt in range(3):
            port = allocate_port()
            try:
                with db.connection() as conn:
                    game_id = conn.execute(
                        "INSERT INTO games(name, seed_name, source, created_at, created_by, job_id, "
                        "generator_version, min_server_version, race_mode, games, port, options) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (display[:120], meta["seed_name"], source, db.now(), created_by, job_id,
                         _version_text(meta["generator_version"]), _version_text(needed) if needed else None,
                         int(meta["race_mode"]), db.jdumps(meta["games"]), port, db.jdumps(options))).lastrowid
                    for slot in meta["slots"]:
                        conn.execute("INSERT INTO game_slots(game_id, slot, name, game, type, locations) "
                                     "VALUES (?,?,?,?,?,?)",
                                     (game_id, slot["slot"], slot["name"], slot["game"], slot["type"],
                                      slot["locations"]))
                    for f in stored:
                        conn.execute("INSERT INTO game_files(game_id, slot, filename, size, kind) VALUES (?,?,?,?,?)",
                                     (game_id, f.slot, f.filename, f.size, f.kind))
                break
            except sqlite3.IntegrityError:
                if attempt == 2:
                    raise
        try:
            gamepaths.game_dir(game_id).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work), gamepaths.game_dir(game_id))
        except OSError:
            db.execute("DELETE FROM games WHERE id = ?", (game_id,))
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)

    link_slots(game_id, owners)
    await asyncio.to_thread(sync_game_yamls, game_id)
    return game_id


def link_slots(game_id: int, owners: dict[str, int] | None = None) -> int:
    """Assigns slots to users by slot name. Returns how many slots were linked."""
    if owners is None:
        candidates: dict[str, set[int]] = {}
        for row in db.query("SELECT user_id, player_names FROM configs"):
            for name in db.jloads(row["player_names"], []):
                candidates.setdefault(name.casefold(), set()).add(row["user_id"])
        owners = {name: next(iter(users)) for name, users in candidates.items() if len(users) == 1}
    linked = 0
    for slot in db.query("SELECT slot, name FROM game_slots WHERE game_id = ? AND type = 1 AND user_id IS NULL",
                         (game_id,)):
        user_id = owners.get(slot["name"].casefold())
        if user_id is not None:
            db.execute("UPDATE game_slots SET user_id = ? WHERE game_id = ? AND slot = ?",
                       (user_id, game_id, slot["slot"]))
            linked += 1
    return linked


def sync_game_yamls(game_id: int, only_slot: int | None = None) -> int:
    """Copies yaml files that came with a game into the library of the user who owns the matching slot.

    Skips yamls the user already has. Returns the number of configs added. Blocking.
    """
    slots = {s["name"].casefold(): s for s in db.query(
        "SELECT slot, name, user_id FROM game_slots WHERE game_id = ? AND user_id IS NOT NULL", (game_id,))}
    added = 0
    for f in db.query("SELECT * FROM game_files WHERE game_id = ? AND kind = 'yaml'", (game_id,)):
        try:
            data = (gamepaths.files_dir(game_id) / f["filename"]).read_bytes()
            _text, info = configstore.analyze_bytes(data, f["filename"])
        except (OSError, yamlinfo.YamlError):
            continue
        owners = {slots[n.casefold()]["user_id"] for n in info.player_names
                  if n.casefold() in slots and (only_slot is None or slots[n.casefold()]["slot"] == only_slot)}
        for user_id in owners:
            if not configstore.has_identical_config(user_id, data):
                try:
                    configstore.store_config(user_id, f["filename"], data, source_game_id=game_id)
                    added += 1
                except yamlinfo.YamlError:
                    pass
    return added


def add_file(game_id: int, filename: str, data_path: Path, *, kind: str | None = None, slot: int | None = None,
             public: bool = False) -> int:
    """Registers a file for a game; `data_path` is moved into place."""
    detected_kind, detected_slot = classify(filename)
    kind = kind or ("other" if detected_kind == "multidata" else detected_kind)
    slot = slot if slot is not None else detected_slot
    directory = gamepaths.files_dir(game_id)
    directory.mkdir(parents=True, exist_ok=True)
    stored_name = _unique_filename(directory, yamlinfo.safe_filename(filename))
    shutil.move(str(data_path), directory / stored_name)
    return db.execute("INSERT INTO game_files(game_id, slot, filename, size, kind, public) VALUES (?,?,?,?,?,?)",
                      (game_id, slot, stored_name, (directory / stored_name).stat().st_size, kind, int(public)))


async def delete_game(game_id: int) -> None:
    await ROOMS.stop(game_id, persist=False)
    ROOMS.forget(game_id)
    db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    await asyncio.to_thread(shutil.rmtree, gamepaths.game_dir(game_id), True)
