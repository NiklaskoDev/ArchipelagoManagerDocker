"""Notices when a newer Archipelago release is out, so an admin knows that updating the image is worthwhile.

The manager never updates itself: Archipelago is part of the image, and an update means pulling a newer image.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request

from . import __version__, apbridge, notify, settings as app_settings
from .config import CONFIG

log = logging.getLogger(__name__)

CHECK_INTERVAL = 12 * 3600
_RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")  # release candidates and other tags are ignored


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.match((text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


class _State:
    latest: str | None = None
    url: str | None = None
    checked_at: float | None = None
    error: str | None = None


STATE = _State()


def _fetch() -> dict:
    request = urllib.request.Request(CONFIG.releases_url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"archipelago-manager/{__version__}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read(1024 * 1024))


async def check_now() -> None:
    try:
        release = await asyncio.to_thread(_fetch)
        tag = str(release.get("tag_name") or "")
        if parse_version(tag) is None:
            raise ValueError(f"unexpected release name {tag!r}")
        STATE.latest, STATE.url, STATE.error = tag.lstrip("v"), release.get("html_url"), None
    except Exception as e:  # noqa: BLE001 - offline servers are normal; report, never fail
        STATE.error = f"Could not check for updates: {e}"
        log.info(STATE.error)
    STATE.checked_at = time.time()
    _announce()


def _announce() -> None:
    """Tells Discord about each new release once. It counts as told only once a message was actually queued."""
    current = status()
    if not current["update_available"] or app_settings.get_state("announced_update") == current["latest"]:
        return
    if notify.emit("ap_update", notify.tr("Archipelago {version} is available", version=current["latest"]),
                   notify.tr("This server runs {installed}. Update the container image to get it: on Unraid, "
                             "Docker → Check for updates → Apply.", installed=current["installed"]),
                   url=current["url"], color="info"):
        app_settings.set_state("announced_update", current["latest"])


async def run_periodically() -> None:
    while True:
        await check_now()
        await asyncio.sleep(CHECK_INTERVAL)


def status() -> dict:
    installed = parse_version(apbridge.ap_version())
    latest = parse_version(STATE.latest)
    return {
        "enabled": CONFIG.update_check,
        "installed": apbridge.ap_version(),
        "latest": STATE.latest,
        "url": STATE.url,
        "checked_at": STATE.checked_at,
        "error": STATE.error,
        "update_available": bool(installed and latest and latest > installed),
    }
