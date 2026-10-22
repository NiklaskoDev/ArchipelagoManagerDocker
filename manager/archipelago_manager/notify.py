"""Discord notifications: a webhook message when something worth knowing happens.

Messages go through a queue and one background task, so a slow or unreachable Discord never holds up the code that
reported the event, and messages arrive in the order they happened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from . import __version__, settings as app_settings

log = logging.getLogger(__name__)

COLORS = {"info": 0x5865F2, "success": 0x57F287, "warning": 0xFEE75C, "error": 0xED4245}
MAX_ATTEMPTS = 4
# Discord allows 5 messages per 2 seconds per webhook; staying below that avoids most 429 answers.
PAUSE_BETWEEN_MESSAGES = 0.5
USER_AGENT = f"DiscordBot (https://github.com/NiklaskoDev/ArchipelagoManagerDocker, {__version__})"


class DeliveryError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None, permanent: bool = False) -> None:
        super().__init__(message)
        self.retry_after, self.permanent = retry_after, permanent


class _State:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.worker: asyncio.Task | None = None
        self.sent = 0
        self.sending = False
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None


STATE = _State()


# ----------------------------------------------------------------------------------------------------------------------
# message building
# ----------------------------------------------------------------------------------------------------------------------
_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")


def md(text: Any) -> str:
    """User-supplied text (game, player and user names) with Discord's markdown characters escaped."""
    return _MARKDOWN.sub(r"\\\1", str(text))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def link(path: str) -> str | None:
    """A link to a page of the web UI, if the admin told us its address."""
    base = app_settings.get_setting("notify_link_url")
    return f"{base.rstrip('/')}/#{path}" if base else None


def game_address(port: int | None) -> str | None:
    """What players type to connect. Without a public address there is nothing sensible to show."""
    host = app_settings.get_setting("public_host")
    if not host:
        host = urllib.parse.urlsplit(app_settings.get_setting("notify_link_url") or "").hostname
    return f"{host}:{port}" if host and port else None


def timestamp(ts: float, style: str = "f") -> str:
    """A Discord timestamp: every reader sees it in their own time zone."""
    return f"<t:{int(ts)}:{style}>"


def build_payload(title: str, description: str = "", *, url: str | None = None,
                  fields: list[tuple] | None = None, color: str = "info") -> dict:
    embed: dict[str, Any] = {"title": _clip(title, 256), "color": COLORS[color],
                             "timestamp": datetime.now(UTC).isoformat()}
    if description:
        embed["description"] = _clip(description, 4000)
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [{"name": _clip(f[0], 256), "value": _clip(f[1], 1024) or "—",
                            "inline": f[2] if len(f) > 2 else False} for f in fields[:25]]
    # Names come from users; never let one of them ping @everyone or a role.
    return {"username": "Archipelago Manager", "embeds": [embed], "allowed_mentions": {"parse": []}}


# ----------------------------------------------------------------------------------------------------------------------
# sending
# ----------------------------------------------------------------------------------------------------------------------
def _discord_reason(body: str) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return str(data.get("message")) if isinstance(data, dict) and data.get("message") else None


def _post(url: str, payload: dict) -> None:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read(64 * 1024)
    except urllib.error.HTTPError as e:
        body = e.read(64 * 1024).decode(errors="replace")
        retry_after = None
        if e.code == 429:
            try:
                retry_after = float(json.loads(body).get("retry_after", 1))
            except (ValueError, AttributeError, TypeError):
                retry_after = float(e.headers.get("Retry-After") or 1)
        reason = _discord_reason(body) or e.reason
        raise DeliveryError(f"The webhook answered {e.code}: {reason}", retry_after=retry_after,
                            permanent=e.code < 500 and e.code != 429) from None
    except (urllib.error.URLError, OSError) as e:
        raise DeliveryError(f"Could not reach the webhook: {getattr(e, 'reason', e)}") from None


async def _deliver(url: str, payload: dict) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_post, url, payload)
        except DeliveryError as e:
            if e.permanent or attempt == MAX_ATTEMPTS:
                STATE.last_error, STATE.last_error_at = str(e), time.time()
                log.warning("Notification not sent: %s", e)
                return
            await asyncio.sleep(min(e.retry_after or 2 ** attempt, 60))
            continue
        STATE.sent += 1
        STATE.last_ok_at = time.time()
        return


async def _work() -> None:
    assert STATE.queue is not None
    while True:
        url, payload = await STATE.queue.get()
        STATE.sending = True
        try:
            await _deliver(url, payload)
        except Exception:  # noqa: BLE001 - a broken message must not stop the ones after it
            log.exception("Sending a notification failed")
        finally:
            STATE.sending = False
        await asyncio.sleep(PAUSE_BETWEEN_MESSAGES)


def start() -> None:
    STATE.loop = asyncio.get_running_loop()
    STATE.queue = asyncio.Queue()
    STATE.worker = asyncio.create_task(_work())


def stop() -> None:
    if STATE.worker:
        STATE.worker.cancel()
    STATE.loop = STATE.queue = STATE.worker = None


def wanted(event: str) -> bool:
    """Whether a message of this kind would be sent, to skip work for messages nobody gets."""
    cfg = app_settings.get_settings()
    return bool(STATE.queue is not None and cfg["discord_webhook_url"] and event in cfg["notify_events"])


def emit(event: str, title: str, description: str = "", *, url: str | None = None,
         fields: list[tuple] | None = None, color: str = "info") -> bool:
    """Queues a message if a webhook is set and the admin wants this kind of event. Safe to call from any thread.

    Returns whether the message was queued.
    """
    if STATE.loop is None or not wanted(event):
        return False
    webhook = app_settings.get_setting("discord_webhook_url")
    payload = build_payload(title, description, url=url, fields=fields, color=color)
    STATE.loop.call_soon_threadsafe(STATE.queue.put_nowait, (webhook, payload))
    return True


async def send_test(webhook: str) -> None:
    """Sends one message right away. Raises DeliveryError with a reason fit for the admin."""
    payload = build_payload("Test message", "Notifications from this Archipelago Manager arrive in this channel.",
                            url=link("/admin/settings"), color="success")
    await asyncio.to_thread(_post, webhook, payload)
    STATE.last_ok_at, STATE.last_error, STATE.last_error_at = time.time(), None, None


def status() -> dict:
    return {"sent": STATE.sent, "last_ok_at": STATE.last_ok_at, "last_error": STATE.last_error,
            "last_error_at": STATE.last_error_at,
            "queued": (STATE.queue.qsize() if STATE.queue else 0) + STATE.sending}
