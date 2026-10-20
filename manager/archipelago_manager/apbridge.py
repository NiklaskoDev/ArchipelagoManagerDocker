"""Talks to the Archipelago installation: runs ap_helper.py in AP's environment and keeps a cache of what it learned."""
from __future__ import annotations

import asyncio
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
    env["XDG_DATA_HOME"] = str(CONFIG.ap_data_home)
    # Requirements are baked into the image. A runtime pip install would only hang waiting for a prompt.
    env["SKIP_REQUIREMENTS_UPDATE"] = "true"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def ap_command(script: str, *args: str) -> list[str]:
    return [CONFIG.ap_python, str(CONFIG.ap_dir / script), *args]


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(SENTINEL):
            return json.loads(line[len(SENTINEL):])
    raise APBridgeError("The Archipelago helper produced no result")


async def run_helper(*args: str, timeout: float = 180) -> dict[str, Any]:
    if not (CONFIG.ap_dir / "Utils.py").is_file():
        raise APBridgeError(f"Archipelago is not installed at {CONFIG.ap_dir}")
    proc = await asyncio.create_subprocess_exec(
        CONFIG.ap_python, str(CONFIG.helper), "--ap-dir", str(CONFIG.ap_dir), *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=ap_env(), cwd=str(CONFIG.ap_dir))
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


def ap_version() -> str | None:
    return (STATE.info or STATE.where or {}).get("version")


def version_tuple() -> tuple[int, ...] | None:
    value = (STATE.info or STATE.where or {}).get("version_tuple")
    return tuple(value) if value else None


def installed_games(*, include_hidden: bool = False) -> dict[str, dict[str, Any]]:
    if not STATE.info:
        return {}
    return {g["name"]: g for g in STATE.info["games"] if include_hidden or not g["hidden"]}


def missing_games(games: list[str] | set[str]) -> list[str]:
    """Games that are not installed. Empty if we cannot tell (Archipelago unavailable)."""
    if not STATE.info:
        return []
    known = {g["name"] for g in STATE.info["games"]}
    return sorted({g for g in games if g and g not in known and g != "Archipelago"})


# ------------------------------------------------------------------------------------------------------------------
# custom worlds on disk
# ------------------------------------------------------------------------------------------------------------------
def apworld_library_path(apworld_id: int) -> Path:
    return CONFIG.apworlds_dir / f"{apworld_id}.apworld"


async def sync_apworlds() -> None:
    """Makes AP's worlds folder match the installed custom worlds in the database."""
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


# ------------------------------------------------------------------------------------------------------------------
# yaml templates
# ------------------------------------------------------------------------------------------------------------------
async def ensure_templates() -> dict[str, str]:
    """Returns {game name: template file name}, generating the templates if the set of worlds changed."""
    async with _templates_lock():
        if STATE.templates is not None:
            return STATE.templates
        info = STATE.info or await refresh_info()
        if not info:
            raise APBridgeError(STATE.error or "Archipelago is unavailable")
        signature = hashlib.sha256(json.dumps(
            [(g["name"], g["version"], g["items"], g["locations"]) for g in info["games"]] + [info["version"]]
        ).encode()).hexdigest()[:16]
        target = CONFIG.templates_dir / signature
        index = target / "index.json"
        if not index.is_file():
            tmp = CONFIG.templates_dir / f"{signature}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            result = await run_helper("templates", str(tmp), timeout=300)
            mapping = {t["game"]: t["file"] for t in result["templates"] if t["game"]}
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
    filename = mapping.get(game)
    if not filename or STATE.templates_dir is None:
        return None
    return (STATE.templates_dir / filename).read_text(encoding="utf-8-sig")
