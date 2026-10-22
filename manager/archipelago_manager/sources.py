"""APWorlds that come from the releases of a GitHub repository.

A player can propose a repository, but nothing is downloaded from it until an administrator approves it: an .apworld
is code that runs on the server. Repositories an administrator adds count as approved. Approved repositories are
checked for new releases twice a day; installing a new release takes an administrator's click again, because it is new
code, and Discord can be told about it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, db, notify
from .config import CONFIG
from .routes import apworlds as library

log = logging.getLogger(__name__)

CHECK_INTERVAL = 12 * 3600
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class SourceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


def parse_repo(text: str) -> str:
    """'owner/name' from what people paste: owner/name, the repository's github.com address, or a link inside it."""
    value = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", (text or "").strip(), flags=re.IGNORECASE)
    parts = value.split("/")
    owner, name = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    name = name[:-4] if name.lower().endswith(".git") else name
    if not _OWNER.match(owner) or not _NAME.match(name) or name in (".", ".."):
        raise SourceError("Enter a GitHub repository as owner/name or as its github.com address")
    return f"{owner}/{name}"


def get(source_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM apworld_sources WHERE id = ?", (source_id,))


# ----------------------------------------------------------------------------------------------------------------------
# GitHub
# ----------------------------------------------------------------------------------------------------------------------
def _api(path: str) -> dict:
    request = urllib.request.Request(f"{CONFIG.github_api}{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"archipelago-manager/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SourceError("not found", 404) from None
        if e.code in (403, 429):
            raise SourceError("GitHub refused the request, most likely its limit of 60 requests an hour for servers "
                              "without an account. Try again later.", 503) from None
        raise SourceError(f"GitHub answered {e.code}", 502) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError(f"Could not reach GitHub: {getattr(e, 'reason', e)}", 502) from None


def fetch_release(repo: str) -> dict:
    """The repository's newest release (drafts and pre-releases are skipped, as GitHub does) and its .apworld files."""
    try:
        info = _api(f"/repos/{repo}")
    except SourceError as e:
        if e.status == 404:
            raise SourceError(f"There is no public GitHub repository {repo}", 404) from None
        raise
    try:
        release = _api(f"/repos/{info['full_name']}/releases/latest")
    except SourceError as e:
        if e.status == 404:
            raise SourceError(f"{info['full_name']} has no published release", 404) from None
        raise
    assets = [{"name": a["name"], "size": a.get("size") or 0, "url": a["browser_download_url"]}
              for a in release.get("assets", []) if str(a.get("name", "")).lower().endswith(".apworld")]
    return {"repo": info["full_name"], "description": info.get("description"),
            "tag": release.get("tag_name") or "", "release_url": release.get("html_url"), "assets": assets}


def _stem(name: str) -> str:
    """An asset name without its version, to recognise the same file in the next release ("x-v1.2.apworld")."""
    return re.sub(r"[-_ .]*v?\d+(?:\.\d+)*(?=[-_ .]|\.apworld$)", "", name.lower())


def pick_asset(release: dict, wanted: str | None) -> dict:
    """The .apworld file to install: the one asked for, the only one, or the one named like before."""
    assets = release["assets"]
    if not assets:
        raise SourceError(f"Release {release['tag']} of {release['repo']} has no .apworld file", 404)
    if wanted:
        for match in ([a for a in assets if a["name"] == wanted], [a for a in assets if _stem(a["name"]) == _stem(wanted)]):
            if len(match) == 1:
                return match[0]
    if len(assets) == 1:
        return assets[0]
    names = ", ".join(a["name"] for a in assets)
    raise SourceError(f"Release {release['tag']} of {release['repo']} has several .apworld files ({names}). "
                      "Choose one.", 409)


def _download(url: str, target: Path) -> None:
    parts = urllib.parse.urlsplit(url)
    allowed = {"github.com", urllib.parse.urlsplit(CONFIG.github_api).hostname}
    if parts.hostname not in allowed or parts.scheme not in ("https", "http") or (
            parts.hostname == "github.com" and parts.scheme != "https"):
        raise SourceError("GitHub returned a download address outside of GitHub", 502)
    request = urllib.request.Request(url, headers={"User-Agent": f"archipelago-manager/{__version__}"})
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > CONFIG.max_upload_bytes:
                    raise SourceError("The .apworld file is too large", 413)
                out.write(chunk)
    except urllib.error.HTTPError as e:
        raise SourceError(f"Downloading the .apworld failed: GitHub answered {e.code}", 502) from None
    except (urllib.error.URLError, OSError) as e:
        raise SourceError(f"Downloading the .apworld failed: {getattr(e, 'reason', e)}", 502) from None


# ----------------------------------------------------------------------------------------------------------------------
# installing and watching for releases
# ----------------------------------------------------------------------------------------------------------------------
async def install(source_id: int) -> dict:
    """Downloads the newest release of an approved source and installs it. Returns the library entry."""
    source = get(source_id)
    if not source or source["status"] != "approved":
        raise SourceError("Only approved sources are installed", 409)
    release = await asyncio.to_thread(fetch_release, source["repo"])
    asset = pick_asset(release, source["asset"])
    tmp = CONFIG.tmp_dir / f"source-{source_id}-{time.time_ns()}.apworld"
    try:
        await asyncio.to_thread(_download, asset["url"], tmp)
        try:
            entry = await library.add_to_library(tmp, asset["name"], uploaded_by=source["added_by"], install=True)
        except library.InvalidAPWorld as e:
            raise SourceError(f"{asset['name']} from {release['repo']} {release['tag']}: {e}") from None
    finally:
        tmp.unlink(missing_ok=True)
    db.execute("UPDATE apworld_sources SET asset = ?, game = ?, installed_tag = ?, apworld_id = ?, latest_tag = ?, "
               "latest_url = ?, checked_at = ?, error = NULL WHERE id = ?",
               (asset["name"], entry["game"], release["tag"], entry["id"], release["tag"], release["release_url"],
                time.time(), source_id))
    return entry


def update_available(source: sqlite3.Row) -> bool:
    return bool(source["status"] == "approved" and source["installed_tag"] and source["latest_tag"]
                and source["latest_tag"] != source["installed_tag"])


async def check(source_id: int) -> None:
    """Looks up the newest release of a source and tells Discord about each new one once."""
    source = get(source_id)
    if not source:
        return
    try:
        release = await asyncio.to_thread(fetch_release, source["repo"])
    except SourceError as e:
        db.execute("UPDATE apworld_sources SET checked_at = ?, error = ? WHERE id = ?", (time.time(), e.message, source_id))
        return
    db.execute("UPDATE apworld_sources SET latest_tag = ?, latest_url = ?, checked_at = ?, error = NULL WHERE id = ?",
               (release["tag"], release["release_url"], time.time(), source_id))
    source = get(source_id)
    if update_available(source) and source["announced_tag"] != source["latest_tag"]:
        game = source["game"] or source["repo"]
        if notify.emit("apworld_update", f"New version of {game}: {source['latest_tag']}",
                       f"{notify.md(source['repo'])} published {notify.md(source['latest_tag'])}; this server has "
                       f"{notify.md(source['installed_tag'])}. An administrator can install it on the Custom games page.",
                       url=notify.link("/worlds"), color="info"):
            db.execute("UPDATE apworld_sources SET announced_tag = ? WHERE id = ?", (source["latest_tag"], source_id))


async def run_periodically() -> None:
    while True:
        for row in db.query("SELECT id FROM apworld_sources WHERE status = 'approved'"):
            try:
                await check(row["id"])
            except Exception:  # noqa: BLE001 - one broken source must not stop the others
                log.exception("Checking the APWorld source %s failed", row["id"])
        await asyncio.sleep(CHECK_INTERVAL)
