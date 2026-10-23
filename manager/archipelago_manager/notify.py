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
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from . import __version__, db, notify_de, settings as app_settings

log = logging.getLogger(__name__)

COLORS = {"info": 0x5865F2, "success": 0x57F287, "warning": 0xFEE75C, "error": 0xED4245}
MAX_ATTEMPTS = 4
# Discord allows 5 messages per 2 seconds per webhook; staying below that avoids most 429 answers.
PAUSE_BETWEEN_MESSAGES = 0.5
USER_AGENT = f"DiscordBot (https://github.com/NiklaskoDev/ArchipelagoManagerDocker, {__version__})"
# What players with a linked Discord account can ask to be mentioned for, and the kind of message that carries it: they
# are only mentioned in messages the admin has switched on.
PING_EVENTS = {"game_ready": "generation", "game_online": "game_online", "hint_found": "hint_found",
               "signup_open": "signup", "signup_closing": "signup"}
PING_KINDS = tuple(PING_EVENTS)
# A message holds 2000 characters, a mention of a Discord id with up to 20 digits takes 24 of them
MAX_MENTIONS = 80
_DISCORD_ID = re.compile(r"[0-9]{1,25}")


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
        # how messages to games' own webhooks went, per game (the fields above are about the main one)
        self.games: dict[int, dict] = {}


STATE = _State()


# ----------------------------------------------------------------------------------------------------------------------
# message building
# ----------------------------------------------------------------------------------------------------------------------
_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")


def md(text: Any) -> str:
    """User-supplied text (game, player and user names) with Discord's markdown characters escaped."""
    return _MARKDOWN.sub(r"\\\1", str(text))


def tr(text: str, **values: Any) -> str:
    """A message text in the language the admin chose for notifications, with the values filled in.

    The English text is the key (notify_de.py has the German ones). Values go in as they are: escape names with md().
    """
    if app_settings.get_setting("notify_language") == "de":
        text = notify_de.MESSAGES.get(text, text)
    return text.format(**values)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def link(path: str) -> str | None:
    """A link to a page of the web UI, if the admin told us its address."""
    base = app_settings.get_setting("site_url")
    return f"{base.rstrip('/')}/#{path}" if base else None


def game_address(port: int | None) -> str | None:
    """What players type to connect. Without a public address there is nothing sensible to show."""
    host = app_settings.get_setting("public_host")
    if not host:
        host = urllib.parse.urlsplit(app_settings.get_setting("site_url") or "").hostname
    return f"{host}:{port}" if host and port else None


def duration(seconds: float) -> str:
    """A playing time such as "2 d 5 h", "3 h 12 min" or "40 min"."""
    minutes = max(0, int(seconds // 60))
    days, hours, mins = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return tr("{d} d {h} h", d=days, h=hours)
    if hours:
        return tr("{h} h {m} min", h=hours, m=mins)
    return tr("{m} min", m=mins)


def race_time(seconds: float) -> str:
    """A race time to the second: "1:23:45", or "23:45" under an hour."""
    total = max(0, int(seconds))
    hours, minutes, secs = total // 3600, total // 60 % 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def timestamp(ts: float, style: str = "f") -> str:
    """A Discord timestamp: every reader sees it in their own time zone."""
    return f"<t:{int(ts)}:{style}>"


def build_payload(title: str, description: str = "", *, url: str | None = None,
                  fields: list[tuple] | None = None, color: str = "info", mentions: list[str] | None = None) -> dict:
    embed: dict[str, Any] = {"title": _clip(title, 256), "color": COLORS[color],
                             "timestamp": datetime.now(UTC).isoformat()}
    if description:
        embed["description"] = _clip(description, 4000)
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [{"name": _clip(f[0], 256), "value": _clip(f[1], 1024) or "—",
                            "inline": f[2] if len(f) > 2 else False} for f in fields[:25]]
    # Names come from users; never let one of them ping @everyone, a role or anyone else. Only the users who asked for
    # it are mentioned, and only by the ids listed here.
    payload: dict[str, Any] = {"username": "Archipelago Manager", "embeds": [embed], "allowed_mentions": {"parse": []}}
    mentions = [m for m in mentions or () if _DISCORD_ID.fullmatch(m)][:MAX_MENTIONS]
    if mentions:
        payload["content"] = " ".join(f"<@{m}>" for m in mentions)
        payload["allowed_mentions"]["users"] = mentions
    return payload


def mentions(kinds: str | tuple[str, ...], users: Collection[int] | None = None) -> list[str]:
    """The Discord ids of the users who asked to be mentioned for one of these kinds of message (of `users`, if given;
    everyone otherwise), unless the admin does not let anyone be mentioned."""
    if not app_settings.get_setting("notify_mentions"):
        return []
    kinds = {kinds} if isinstance(kinds, str) else set(kinds)
    found = []
    for row in db.query("SELECT id, discord_id, discord_pings FROM users "
                        "WHERE discord_id IS NOT NULL AND disabled = 0 ORDER BY id"):
        if (users is None or row["id"] in users) and kinds & set(db.jloads(row["discord_pings"], [])):
            found.append(row["discord_id"])
    return found


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


def _note(game_id: int | None, error: str | None = None) -> None:
    """Remembers how the last message to the main webhook (game_id None) or to a game's own one went."""
    now = time.time()
    if game_id is None:
        if error:
            STATE.last_error, STATE.last_error_at = error, now
        else:
            STATE.last_ok_at = now
        return
    entry = STATE.games.setdefault(game_id, {"last_ok_at": None, "last_error": None, "last_error_at": None})
    if error:
        entry["last_error"], entry["last_error_at"] = error, now
    else:
        entry["last_ok_at"] = now


async def _deliver(url: str, payload: dict, game_id: int | None) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_post, url, payload)
        except DeliveryError as e:
            if e.permanent or attempt == MAX_ATTEMPTS:
                _note(game_id, str(e))
                log.warning("Notification not sent%s: %s", f" to the webhook of game {game_id}" if game_id else "", e)
                return
            await asyncio.sleep(min(e.retry_after or 2 ** attempt, 60))
            continue
        STATE.sent += 1
        _note(game_id)
        return


async def _work() -> None:
    assert STATE.queue is not None
    while True:
        url, payload, game_id = await STATE.queue.get()
        STATE.sending = True
        try:
            await _deliver(url, payload, game_id)
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


def game_webhook(game_id: int | None) -> str | None:
    """The game's own webhook, if an admin gave it one."""
    if game_id is None:
        return None
    row = db.query_one("SELECT discord_webhook_url FROM games WHERE id = ?", (game_id,))
    return row["discord_webhook_url"] if row and row["discord_webhook_url"] else None


def wanted(event: str, game_id: int | None = None) -> bool:
    """Whether a message of this kind would be sent, to skip work for messages nobody gets."""
    cfg = app_settings.get_settings()
    return bool(STATE.queue is not None and event in cfg["notify_events"]
                and (game_webhook(game_id) or cfg["discord_webhook_url"]))


def emit(event: str, title: str, description: str = "", *, url: str | None = None,
         fields: list[tuple] | None = None, color: str = "info", ping: str | tuple[str, ...] | None = None,
         ping_users: Collection[int] | None = None, game_id: int | None = None) -> bool:
    """Queues a message if a webhook is set and the admin wants this kind of event. Safe to call from any thread.

    `ping` mentions the users who asked to be mentioned for that kind of message (PING_KINDS), of `ping_users` if given.
    A message about a game (`game_id`) goes to the game's own webhook instead of the main one, if it has one.
    Returns whether the message was queued.
    """
    if STATE.loop is None or not wanted(event, game_id):
        return False
    own = game_webhook(game_id)
    webhook = own or app_settings.get_setting("discord_webhook_url")
    payload = build_payload(title, description, url=url, fields=fields, color=color,
                            mentions=mentions(ping, ping_users) if ping else None)
    STATE.loop.call_soon_threadsafe(STATE.queue.put_nowait, (webhook, payload, game_id if own else None))
    return True


async def send_test(webhook: str, *, game_id: int | None = None, game_name: str | None = None) -> None:
    """Sends one message right away, to the main webhook or to a game's own. Raises DeliveryError with a reason fit for
    the admin."""
    if game_id is None:
        payload = build_payload(tr("Test message"),
                                tr("Notifications from this Archipelago Manager arrive in this channel."),
                                url=link("/admin/settings"), color="success")
    else:
        payload = build_payload(tr("Test message"), tr("Messages about {game} arrive in this channel.",
                                                       game=md(game_name)),
                                url=link(f"/games/{game_id}"), color="success")
    await asyncio.to_thread(_post, webhook, payload)
    if game_id is None:
        STATE.last_ok_at, STATE.last_error, STATE.last_error_at = time.time(), None, None
    else:
        STATE.games[game_id] = {"last_ok_at": time.time(), "last_error": None, "last_error_at": None}


def game_status(game_id: int) -> dict | None:
    """How the last message to the game's own webhook went, if one was sent since the manager started."""
    return STATE.games.get(game_id)


def forget_game(game_id: int) -> None:
    """After the game's webhook changed, or the game was deleted."""
    STATE.games.pop(game_id, None)


def ping_kinds_offered(values: dict[str, Any] | None = None) -> list[str]:
    """The kinds of mention players can ask for: those whose messages are sent."""
    values = values or app_settings.get_settings()
    if not (values["discord_webhook_url"] and values["notify_mentions"]):
        return []
    return [kind for kind, event in PING_EVENTS.items() if event in values["notify_events"]]


def status() -> dict:
    return {"sent": STATE.sent, "last_ok_at": STATE.last_ok_at, "last_error": STATE.last_error,
            "last_error_at": STATE.last_error_at,
            "queued": (STATE.queue.qsize() if STATE.queue else 0) + STATE.sending}
