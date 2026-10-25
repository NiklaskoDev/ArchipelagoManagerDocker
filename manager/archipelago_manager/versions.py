"""Further Archipelago versions besides the one in the image, so that every game can run on the version it needs.

An administrator installs a release by its number. The manager downloads its source (AP_SOURCE_URL, GitHub by default)
into ap-versions/<version>/archipelago in the data folder, makes a Python environment for it next to it (venv) with
Archipelago's requirements and those of its worlds, the way the image does for its own version, and checks that it
loads. Archipelago can write to that folder, so it keeps its host.yaml and custom worlds there, apart from the other
versions: custom games go into every version they say they work with (apbridge.sync_version_apworlds), and the base
ROMs of the image's version are linked in (apbridge.link_roms).

The versions are not part of the backup of everything: they are downloaded again in a few minutes, and a game whose
version is missing says so until it is installed again or the game is moved to another version.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, apbridge, db
from .config import CONFIG

log = logging.getLogger(__name__)

VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
STEP_TIMEOUT = 30 * 60
LOG_TAIL_BYTES = 64 * 1024
# GUI toolkits a headless server does without (as in the Dockerfile)
_SKIPPED_REQUIREMENTS = re.compile(r"^\s*(kivy|kivymd|pyshortcuts)\b", re.IGNORECASE)


class VersionError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class _State:
    task: asyncio.Task | None = None


STATE = _State()


def root() -> Path:
    return CONFIG.data_dir / "ap-versions"


def _pip_cache() -> Path:  # one installation at a time
    return CONFIG.tmp_dir / "pip-cache"


def log_path(version: str) -> Path:
    return root() / f"{version}.log"


def present(version: str) -> bool:
    """Whether a version's files are there (they are not after restoring a backup of everything)."""
    folder, python = apbridge.install_of(version)
    return (folder / "Utils.py").is_file() and Path(python).exists()


def available(version: str) -> bool:
    row = db.query_one("SELECT status FROM ap_versions WHERE version = ?", (version,))
    return bool(row and row["status"] == "installed" and present(version))


def check_choice(version: str | None) -> str | None:
    """The version a game or a generation is to use: None for the image's. Raises VersionError for one that is not
    installed."""
    version = (version or "").strip() or None
    if version is None:
        return None
    if not VERSION.match(version) or not available(version):
        raise VersionError(f"Archipelago {version} is not installed on this server", 409)
    return version


def unavailable_text(version: str) -> str:
    return (f"Archipelago {version} is not installed on this server. Install it again under Admin → Settings → "
            f"Archipelago versions, or choose another version in the game's settings.")


def reset() -> None:
    """At startup: an installation the last run did not finish failed."""
    db.execute("UPDATE ap_versions SET status = 'failed', error = 'The installation was interrupted by a restart' "
               "WHERE status = 'installing'")


def listing() -> dict[str, Any]:
    used: dict[str, list[str]] = {}
    for row in db.query("SELECT name, ap_version FROM games WHERE ap_version IS NOT NULL ORDER BY name"):
        used.setdefault(row["ap_version"], []).append(row["name"])
    versions = []
    for row in db.query("SELECT * FROM ap_versions"):
        status = row["status"]
        if status == "installed" and not present(row["version"]):
            status = "missing"
        info = apbridge.EXTRA.get(row["version"])
        versions.append({
            "version": row["version"], "status": status, "error": row["error"], "created_at": row["created_at"],
            "installed_at": row["installed_at"], "size": row["size"], "games": used.get(row["version"], []),
            "world_count": len(info["games"]) if info else None,
            "failed_worlds": sorted((info or {}).get("failed", {})),
        })
    versions.sort(key=lambda v: apbridge.version_tuple(v["version"]) or (), reverse=True)
    return {"default": apbridge.ap_version(), "versions": versions}


def log_tail(version: str) -> str:
    try:
        with open(log_path(version), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            return f.read().decode(errors="replace")
    except FileNotFoundError:
        return ""


# ----------------------------------------------------------------------------------------------------------------------
# installing and removing
# ----------------------------------------------------------------------------------------------------------------------
def start_install(version: str) -> None:
    version = version.strip().lstrip("v")
    if not VERSION.match(version):
        raise VersionError("A version looks like 0.6.2")
    if version == apbridge.ap_version():
        raise VersionError(f"Archipelago {version} is the version this server comes with")
    if STATE.task and not STATE.task.done():
        raise VersionError("Another version is being installed. Wait until it is done.", 409)
    row = db.query_one("SELECT status FROM ap_versions WHERE version = ?", (version,))
    if row and row["status"] == "installed" and present(version):
        raise VersionError(f"Archipelago {version} is already installed", 409)
    db.execute("INSERT INTO ap_versions(version, status, created_at) VALUES (?, 'installing', ?) "
               "ON CONFLICT(version) DO UPDATE SET status = 'installing', error = NULL, created_at = excluded.created_at",
               (version, time.time()))
    STATE.task = asyncio.create_task(_install(version))


async def _install(version: str) -> None:
    folder = apbridge.version_dir(version)
    root().mkdir(parents=True, exist_ok=True)
    with open(log_path(version), "wb") as out:
        def note(text: str) -> None:
            out.write(f"--- {text}\n".encode())
            out.flush()

        try:
            note(f"installing Archipelago {version}")
            await asyncio.to_thread(shutil.rmtree, folder, True)
            source = folder / "archipelago"
            url = CONFIG.ap_source_url.format(version=version)
            note(f"downloading {url}")
            await asyncio.to_thread(_download, url, source)

            note("making its Python environment")
            await _run(out, [CONFIG.ap_python, "-m", "venv", str(folder / "venv")])
            python = apbridge.install_of(version)[1]
            core = folder / "core-requirements.txt"
            core.write_text("".join(line for line in (source / "requirements.txt").read_text().splitlines(True)
                                    if not _SKIPPED_REQUIREMENTS.match(line)))
            note("installing Archipelago's requirements")
            await _run(out, [python, "-m", "pip", "install", "-r", str(core), "setuptools>=75,<81", "requests"])
            await _install_world_requirements(out, note, python, source, core)
            note("building the speedups (optional: without a C compiler Archipelago does without them)")
            with contextlib.suppress(VersionError):
                await _run(out, [python, "-m", "Cython.Build.Cythonize", "-b", "-i", "_speedups.pyx"], cwd=source)
            for name in ("Players", "custom_worlds"):
                (source / name).mkdir(exist_ok=True)
            (source / "manifest.json").write_text(f'{{"version": "{version}"}}\n')

            note("checking that it loads")
            apbridge.EXTRA.pop(version, None)
            apbridge.sync_version_apworlds(version)
            apbridge.link_roms(version)
            info = await apbridge.extra_info(version)
            if info.get("version") != version:
                raise VersionError(f"The download is Archipelago {info.get('version')}, not {version}")
            size = await asyncio.to_thread(_size, folder)
            db.execute("UPDATE ap_versions SET status = 'installed', installed_at = ?, size = ?, error = NULL "
                       "WHERE version = ?", (time.time(), size, version))
            note(f"done: {len(info['games'])} games")
            log.info("Installed Archipelago %s", version)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, the version is marked failed and says why
            message = e.message if isinstance(e, VersionError) else (str(e) or type(e).__name__)
            if isinstance(e, apbridge.APBridgeError):
                message = f"It does not load: {e}"
            note(f"failed: {message}")
            log.warning("Installing Archipelago %s failed: %s", version, message)
            apbridge.EXTRA.pop(version, None)
            await asyncio.to_thread(shutil.rmtree, folder, True)
            db.execute("UPDATE ap_versions SET status = 'failed', error = ? WHERE version = ?", (message[:2000], version))
        finally:
            shutil.rmtree(_pip_cache(), ignore_errors=True)


async def _install_world_requirements(out: BinaryIO, note, python: str, source: Path, core: Path) -> None:
    """The worlds' own requirements, all at once; if they do not go together, one world after the other, so that one
    world's trouble costs only that world (it then fails to load and says so)."""
    files = sorted(source.glob("worlds/*/requirements.txt"))
    if not files:
        return
    note("installing the worlds' requirements")
    together = [arg for f in files for arg in ("-r", str(f))]
    try:
        await _run(out, [python, "-m", "pip", "install", *together, "--constraint", str(core)])
        return
    except VersionError:
        note("not all at once; one world after the other")
    for f in files:
        try:
            await _run(out, [python, "-m", "pip", "install", "-r", str(f), "--constraint", str(core)])
        except VersionError:
            note(f"skipped the requirements of {f.parent.name}: that world will not load")


async def _run(out: BinaryIO, cmd: list[str], *, cwd: Path | None = None) -> None:
    out.write(("$ " + " ".join(cmd) + "\n").encode())
    out.flush()
    env = apbridge.ap_env()
    env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
                # not in the data folder's home, where it would stay and be backed up
                "PIP_CACHE_DIR": str(_pip_cache())})
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=out,
                                                stderr=asyncio.subprocess.STDOUT, cwd=str(cwd) if cwd else None,
                                                env=env, start_new_session=True)
    try:
        code = await asyncio.wait_for(proc.wait(), STEP_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise VersionError(f"{Path(cmd[0]).name} took longer than {STEP_TIMEOUT // 60} minutes") from None
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if code != 0:
        raise VersionError(f"{' '.join(Path(part).name for part in cmd[:4])} failed (exit code {code}); see the log")


def _download(url: str, target: Path) -> None:
    """Fetches a source archive (.tar.gz, with everything in one top folder, as GitHub makes them) and unpacks it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.parent / "source.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": f"archipelago-manager/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as f:
            size = 0
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise VersionError("The download is unreasonably large")
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise VersionError("There is no Archipelago release of that number" if e.code == 404
                           else f"The download failed: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VersionError(f"The download failed: {getattr(e, 'reason', None) or e}") from None
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            tops = {m.name.split("/", 1)[0] for m in members}
            if len(tops) != 1:
                raise VersionError("The download is not an Archipelago source archive")
            top = tops.pop()
            unpack = target.parent / "unpack"
            tar.extractall(unpack, filter="data")
        (unpack / top).rename(target)
        shutil.rmtree(unpack, ignore_errors=True)
    except (tarfile.TarError, OSError) as e:
        raise VersionError(f"The download could not be unpacked: {e}") from None
    finally:
        archive.unlink(missing_ok=True)
    if not (target / "Utils.py").is_file() or not (target / "requirements.txt").is_file():
        raise VersionError("The download is not an Archipelago source archive")


def _size(path: Path) -> int:
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(folder, name)).st_size
    return total


async def remove(version: str) -> None:
    row = db.query_one("SELECT status FROM ap_versions WHERE version = ?", (version,))
    if not row:
        raise VersionError("No such version", 404)
    if row["status"] == "installing":
        raise VersionError("This version is being installed. Wait until it is done.", 409)
    games = [r["name"] for r in db.query("SELECT name FROM games WHERE ap_version = ? ORDER BY name", (version,))]
    if games:
        raise VersionError(f"These games run on Archipelago {version}: {', '.join(games)}. Choose another version "
                           f"for them first.", 409)
    db.execute("DELETE FROM ap_versions WHERE version = ?", (version,))
    apbridge.EXTRA.pop(version, None)
    await asyncio.to_thread(shutil.rmtree, apbridge.version_dir(version), True)
    log_path(version).unlink(missing_ok=True)
