"""Talks to the Archipelago installation: runs ap_helper.py in AP's environment and keeps a cache of what it learned.

Besides the version in the image (CONFIG.ap_dir), further versions can be installed (versions.py). Functions that take
a `version` work on that one; None is the image's.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from . import db
from .config import CONFIG

log = logging.getLogger(__name__)

SENTINEL = "@@APJSON@@"


class APBridgeError(RuntimeError):
    pass


def ap_env() -> dict[str, str]:
    """Environment for every Archipelago subprocess (helper, generator, servers)."""
    env = os.environ.copy()
    env.pop("ADMIN_PASSWORD", None)  # the manager's own login is none of an .apworld's business
    env["XDG_DATA_HOME"] = str(CONFIG.ap_data_home)
    # Requirements are baked into the image. A runtime pip install would only hang waiting for a prompt.
    env["SKIP_REQUIREMENTS_UPDATE"] = "true"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def version_dir(version: str) -> Path:
    """Where a further version lives: its source in archipelago/, its Python environment in venv/."""
    return CONFIG.data_dir / "ap-versions" / version


def install_of(version: str | None) -> tuple[Path, str]:
    """The source folder and the Python of an Archipelago version (None: the image's)."""
    if not version:
        return CONFIG.ap_dir, CONFIG.ap_python
    folder = version_dir(version)
    return folder / "archipelago", str(folder / "venv" / "bin" / "python")


def ap_dir(version: str | None = None) -> Path:
    return install_of(version)[0]


def ap_command(script: str, *args: str, version: str | None = None) -> list[str]:
    folder, python = install_of(version)
    return [python, str(folder / script), *args]


def game_version(game_id: int) -> str | None:
    """The Archipelago version a game runs on (None: the image's)."""
    row = db.query_one("SELECT ap_version FROM games WHERE id = ?", (game_id,))
    return row["ap_version"] if row else None


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(SENTINEL):
            return json.loads(line[len(SENTINEL):])
    raise APBridgeError("The Archipelago helper produced no result")


# Each helper loads Archipelago and often a whole multiworld into memory. However many requests come in at once, only a
# few run at the same time; the rest wait their turn.
_HELPERS = asyncio.Semaphore(4)


async def run_helper(*args: str, timeout: float = 180, version: str | None = None) -> dict[str, Any]:
    folder, python = install_of(version)
    if not (folder / "Utils.py").is_file():
        if version:
            raise APBridgeError(f"Archipelago {version} is not installed on this server")
        raise APBridgeError(f"Archipelago is not installed at {CONFIG.ap_dir}")
    async with _HELPERS:
        proc = await asyncio.create_subprocess_exec(
            python, str(CONFIG.helper), "--ap-dir", str(folder), *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=ap_env(), cwd=str(folder))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise APBridgeError(f"Archipelago helper timed out after {timeout:.0f}s") from None
    try:
        result = _parse_result(stdout.decode(errors="replace"))
    except (APBridgeError, ValueError):
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise APBridgeError("The Archipelago helper failed: " + (" | ".join(tail) or f"exit code {proc.returncode}")) from None
    if not result.get("ok"):
        raise APBridgeError(result.get("error") or "Unknown Archipelago helper error")
    return result


# ------------------------------------------------------------------------------------------------------------------
# installation info (version, installed games)
# ------------------------------------------------------------------------------------------------------------------
class _State:
    info: dict[str, Any] | None = None
    error: str | None = None
    where: dict[str, Any] | None = None
    lock: asyncio.Lock | None = None
    templates_lock: asyncio.Lock | None = None
    quarantined: list[str] = []  # files moved out of the worlds folder because they would break Archipelago
    templates: dict[str, str] | None = None
    templates_dir: Path | None = None


STATE = _State()
# What each further version reported about itself (its "games"), loaded when first needed; dropped when its custom
# games change
EXTRA: dict[str, dict[str, Any]] = {}
_EXTRA_LOCKS: dict[str, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    if STATE.lock is None:
        STATE.lock = asyncio.Lock()
    return STATE.lock


def _templates_lock() -> asyncio.Lock:
    if STATE.templates_lock is None:
        STATE.templates_lock = asyncio.Lock()
    return STATE.templates_lock


async def get_where() -> dict[str, Any]:
    """Where Archipelago wants custom worlds and host.yaml to live. Cheap, so it can run before worlds are loaded."""
    if STATE.where is None:
        STATE.where = await run_helper("where", timeout=60)
    return STATE.where


async def refresh_info() -> dict[str, Any] | None:
    """(Re)loads the list of installed games. Returns None, with STATE.error set, when AP is unusable."""
    async with _lock():
        try:
            STATE.info = await run_helper("games", timeout=180)
            STATE.error = None
            STATE.templates = None  # world set may have changed
        except APBridgeError as e:
            STATE.error = str(e)
            log.error("Could not query Archipelago: %s", e)
        return STATE.info


async def extra_info(version: str) -> dict[str, Any]:
    """What a further version reports about itself: its games. Raises APBridgeError when it cannot be loaded."""
    async with _EXTRA_LOCKS.setdefault(version, asyncio.Lock()):
        if version not in EXTRA:
            EXTRA[version] = await run_helper("games", timeout=300, version=version)
        return EXTRA[version]


def info_of(version: str | None) -> dict[str, Any] | None:
    return EXTRA.get(version) if version else STATE.info


def ap_version(version: str | None = None) -> str | None:
    if version:
        return version
    return (STATE.info or STATE.where or {}).get("version")


def version_tuple(version: str | None = None) -> tuple[int, ...] | None:
    if version:  # further versions are named by their number (versions.VERSION)
        return parse_version(version)
    value = (STATE.info or STATE.where or {}).get("version_tuple")
    return tuple(value) if value else None


def installed_games(*, include_hidden: bool = False, version: str | None = None) -> dict[str, dict[str, Any]]:
    info = info_of(version)
    if not info:
        return {}
    return {g["name"]: g for g in info["games"] if include_hidden or not g["hidden"]}


def describe_file(path: str | Path) -> dict[str, Any]:
    """A file's name and folder, the folder relative to the data folder when it is inside it."""
    path = Path(path)
    try:
        folder, in_data_dir = path.parent.relative_to(CONFIG.data_dir).as_posix(), True
    except ValueError:
        folder, in_data_dir = path.parent.as_posix(), False
    return {"file": path.name, "folder": folder, "in_data_dir": in_data_dir}


def file_location_text(item: dict[str, Any]) -> str:
    if not item["in_data_dir"]:
        return f'"{item["file"]}" into {item["folder"]}/'
    if item["folder"] == ".":
        return f'"{item["file"]}" into the data folder'
    return f'"{item["file"]}" into {item["folder"]}/ in the data folder'


def roms() -> list[dict[str, Any]]:
    """Every base ROM the installed games ask for, whether it is there and whether generating needs it.

    These are the games' file settings in host.yaml with known checksums (an upload is checked against them), plus
    files a custom game looked for without such a setting. Whether a file is there is checked now, so a ROM put into
    place counts at once; whether it is the right one was checked when the games were loaded or it was uploaded.
    """
    result = []
    for name, game in sorted(installed_games(include_hidden=True).items(), key=lambda kv: kv[0].lower()):
        paths = set()
        for rom in game.get("roms") or []:
            exists = os.path.exists(rom["path"])
            paths.add(rom["path"])
            result.append({"game": name, "setting": rom["setting"], "path": rom["path"], **describe_file(rom["path"]),
                           "description": rom["description"], "checked": True, "needed": rom["needed"],
                           "exists": exists, "valid": rom["valid"] if exists else None})
        need = game.get("needs_file")
        if need and need.get("path") and os.path.abspath(need["path"]) not in paths:
            exists = os.path.exists(need["path"])
            result.append({"game": name, "setting": None, "path": need["path"], **describe_file(need["path"]),
                           "description": "", "checked": False, "needed": True, "exists": exists, "valid": None})
    return result


def rom_checked(game: str, setting: str | None, valid: bool | None) -> None:
    """Notes the outcome of checking a ROM that was just put into place (or removed: None)."""
    for rom in (installed_games(include_hidden=True).get(game) or {}).get("roms") or []:
        if rom["setting"] == setting:
            rom["valid"] = valid


def missing_files(games: list[str] | set[str]) -> list[dict[str, Any]]:
    """Games among these that cannot be generated yet because a file they need, their base ROM, is not on the server."""
    wanted = set(games)
    result, seen = [], set()
    for rom in roms():
        if rom["game"] in wanted and rom["needed"] and not rom["exists"] and rom["game"] not in seen:
            seen.add(rom["game"])
            result.append({key: rom[key] for key in ("game", "setting", "file", "folder", "in_data_dir")})
    return result


def missing_file_text(item: dict[str, Any]) -> str:
    return f"{item['game']} needs its base ROM on the server to generate: put {file_location_text(item)}."


def missing_games(games: list[str] | set[str], version: str | None = None) -> list[str]:
    """Games that are not installed (in that version). Empty if we cannot tell (Archipelago unavailable)."""
    info = info_of(version)
    if not info:
        return []
    known = {g["name"] for g in info["games"]}
    return sorted({g for g in games if g and g not in known and g != "Archipelago"})


# ------------------------------------------------------------------------------------------------------------------
# custom worlds on disk
# ------------------------------------------------------------------------------------------------------------------
def apworld_library_path(apworld_id: int) -> Path:
    return CONFIG.apworlds_dir / f"{apworld_id}.apworld"


async def sync_apworlds() -> None:
    """Makes AP's worlds folder match the installed custom worlds in the database, in every version."""
    where = await get_where()
    folder = Path(where["worlds_folder"])
    folder.mkdir(parents=True, exist_ok=True)
    for row in db.query("SELECT id, module FROM apworlds WHERE status = 'installed'"):
        source = apworld_library_path(row["id"])
        target = folder / f"{row['module']}.apworld"
        if not source.is_file():
            log.warning("APWorld %s is missing from the library", source)
            continue
        if not target.is_file() or target.stat().st_size != source.stat().st_size \
                or _sha256(target) != _sha256(source):
            shutil.copyfile(source, target)
    for row in db.query("SELECT version FROM ap_versions WHERE status = 'installed'"):
        if (ap_dir(row["version"]) / "Utils.py").is_file():
            sync_version_apworlds(row["version"])


def parse_version(text: Any) -> tuple[int, ...] | None:
    """(0, 6, 2) for "0.6.2"; None for nothing or something else."""
    if not str(text).strip():
        return None
    try:
        return tuple(int(part) for part in str(text).strip().lstrip("v").split("."))
    except ValueError:
        return None


def works_with(manifest: dict, version: tuple[int, ...] | None) -> bool:
    """Whether a custom world says it works with this Archipelago version (it does if it says nothing)."""
    if not version:
        return True
    low = parse_version(manifest.get("minimum_ap_version") or "")
    high = parse_version(manifest.get("maximum_ap_version") or "")
    return not (low and version < low) and not (high and version > high)


def sync_version_apworlds(version: str) -> None:
    """Makes a further version's custom_worlds folder hold exactly the custom worlds that work with it. The folder is
    the manager's alone: Archipelago uses its own folder when it can write to it."""
    folder = ap_dir(version) / "custom_worlds"
    folder.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, Path] = {}
    for row in db.query("SELECT id, module, manifest FROM apworlds WHERE status = 'installed'"):
        source = apworld_library_path(row["id"])
        if source.is_file() and works_with(db.jloads(row["manifest"], {}) or {}, version_tuple(version)):
            wanted[f"{row['module']}.apworld"] = source
    changed = False
    for present in folder.glob("*.apworld"):
        if present.name not in wanted:
            present.unlink()
            changed = True
    for name, source in wanted.items():
        target = folder / name
        if not target.is_file() or target.stat().st_size != source.stat().st_size or _sha256(target) != _sha256(source):
            shutil.copyfile(source, target)
            changed = True
    if changed:
        EXTRA.pop(version, None)


def link_roms(version: str) -> None:
    """Lets a further version find the base ROMs the image's version has: a link in its own folder, under the name
    the file has there (Archipelago looks for them in its user folder, which for a further version is its own)."""
    for rom in roms():
        source = Path(rom["path"])
        target = ap_dir(version) / source.name
        if rom["exists"] and source.is_file() and not target.exists():
            with contextlib.suppress(OSError):
                target.symlink_to(source)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def remove_apworld_file(module: str) -> None:
    where = await get_where()
    target = Path(where["worlds_folder"]) / f"{module}.apworld"
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    for row in db.query("SELECT version FROM ap_versions"):
        extra = ap_dir(row["version"]) / "custom_worlds" / f"{module}.apworld"
        if extra.is_file():
            extra.unlink()
            EXTRA.pop(row["version"], None)


# ------------------------------------------------------------------------------------------------------------------
# yaml templates
# ------------------------------------------------------------------------------------------------------------------
# Raise when the helper's templates run writes something new, so folders written by an older manager are made again.
TEMPLATES_FORMAT = 2


async def ensure_templates() -> dict[str, dict[str, str]]:
    """Returns {"templates": {game: template file}, "forms": {game: options form file}}, generating both when the set
    of worlds changed."""
    async with _templates_lock():
        if STATE.templates is not None:
            return STATE.templates
        info = STATE.info or await refresh_info()
        if not info:
            raise APBridgeError(STATE.error or "Archipelago is unavailable")
        # A custom world installed again gets a new library entry, so its options count as new even when its version
        # and its numbers of items and locations stay the same.
        custom = [(r["id"], r["module"]) for r in db.query("SELECT id, module FROM apworlds WHERE status = 'installed' "
                                                            "ORDER BY id")]
        signature = hashlib.sha256(json.dumps(
            [(g["name"], g["version"], g["items"], g["locations"]) for g in info["games"]]
            + [info["version"], TEMPLATES_FORMAT, custom]
        ).encode()).hexdigest()[:16]
        target = CONFIG.templates_dir / signature
        index = target / "index.json"
        if not index.is_file():
            tmp = CONFIG.templates_dir / f"{signature}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            result = await run_helper("templates", str(tmp), timeout=300)
            if result.get("templates_error"):
                log.warning("Some yaml templates are missing: %s", result["templates_error"])
            for game, error in result.get("form_errors", {}).items():
                log.warning("No options form for %s: %s", game, error)
            mapping = {"templates": {t["game"]: t["file"] for t in result["templates"] if t["game"]},
                       "forms": result.get("forms", {})}
            (tmp / "index.json").write_text(json.dumps(mapping))
            shutil.rmtree(target, ignore_errors=True)
            tmp.rename(target)
            for old in CONFIG.templates_dir.iterdir():
                if old.is_dir() and old.name != signature:
                    shutil.rmtree(old, ignore_errors=True)
        STATE.templates_dir = target
        STATE.templates = json.loads(index.read_text())
        return STATE.templates


async def template_text(game: str) -> str | None:
    mapping = await ensure_templates()
    filename = mapping["templates"].get(game)
    if not filename or STATE.templates_dir is None:
        return None
    return (STATE.templates_dir / filename).read_text(encoding="utf-8-sig")


async def options_form_path(game: str) -> Path | None:
    """The file describing a game's options for the form editor (JSON), or None if the game has none."""
    mapping = await ensure_templates()
    filename = mapping["forms"].get(game)
    if not filename or STATE.templates_dir is None:
        return None
    return STATE.templates_dir / "forms" / filename
