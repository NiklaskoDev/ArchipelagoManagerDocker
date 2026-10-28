"""Calendar files (iCalendar, RFC 5545): a game's planned start and a sign-up round's deadline, for players to add to
their calendar, with a reminder before.

A file is a snapshot of the time it was downloaded at. Its event has the same UID every time, so a calendar can tell
that a file downloaded again after the time moved is the same event.
"""
from __future__ import annotations

import re
import time
import urllib.parse

from fastapi import Request, Response

from . import settings as app_settings

LINE_OCTETS = 75  # a longer line goes on in the next one, which starts with a space
LANGUAGES = ("en", "de", "fr", "nl")  # those of the web UI, which asks for the file in its own (notify.tr)


def _when(ts: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


def _text(value: str) -> str:
    """A TEXT value: control characters left out, line breaks as \\n, and the characters that separate values escaped."""
    value = "".join(ch for ch in value.replace("\r\n", "\n") if ch == "\n" or ch.isprintable())
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    parts, current, limit = [], b"", LINE_OCTETS
    for ch in line:  # never inside a character's UTF-8 bytes
        encoded = ch.encode()
        if len(current) + len(encoded) > limit:
            parts.append(current)
            current, limit = b"", LINE_OCTETS - 1
        current += encoded
    parts.append(current)
    return "\r\n ".join(part.decode() for part in parts)


def event(*, uid: str, at: float, summary: str, description: str, url: str | None, remind_minutes: int) -> str:
    """A calendar with one event at the moment `at` (a start or a deadline has no length), and a reminder
    `remind_minutes` before it."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Archipelago Manager//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{_when(time.time())}", f"DTSTART:{_when(at)}",
             f"SUMMARY:{_text(summary)}"]
    if description:
        lines.append(f"DESCRIPTION:{_text(description)}")
    if url:
        lines.append(f"URL:{url}")
    lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_text(summary)}", f"TRIGGER:-PT{max(0, remind_minutes)}M",
              "END:VALARM", "END:VEVENT", "END:VCALENDAR"]
    return "".join(f"{_fold(line)}\r\n" for line in lines)


def site(request: Request) -> tuple[str, str]:
    """The address of the web UI (the configured one, else the one asked) and its host name, for links and UIDs."""
    base = (app_settings.get_setting("site_url") or str(request.base_url)).rstrip("/")
    return base, urllib.parse.urlsplit(base).hostname or "archipelago-manager"


def response(calendar: str, name: str) -> Response:
    """The file, named after what it is for. Browsers hand it to the calendar app, or download it."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:60] or "event"
    return Response(calendar, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{stem}.ics"'})
