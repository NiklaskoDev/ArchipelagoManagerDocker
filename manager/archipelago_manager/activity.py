"""What happens in a multiworld, for every player of the game: chat, items sent and found, hints, joins and goals.

Read from the server's log as it runs (rooms._pump), the way Archipelago's text clients show it, and kept with the game,
so it is there for players who were not connected. Only these kinds of lines are kept: the server's other output,
console commands, remote administration and the private answers to one client are not. Secrets are already masked
(rooms.mask_secrets) by the time a line gets here.

Each entry is a line of JSON in the game's folder with the time it happened; the latest ones are also kept in memory.
"""
from __future__ import annotations

import collections
import contextlib
import json
import re
import time
from dataclasses import dataclass, field

from . import gamepaths

KEEP = 1000                  # entries in memory, and the most a first look returns
FILE_LIMIT = 2 * 1024 * 1024  # then the file starts over, keeping one older one

_PLAYER = r"(?P<player>.+) \(Team #\d+\)"
# The first rule that matches a line decides; None keeps nothing
_RULES: list[tuple[str | None, re.Pattern]] = [
    ("goal", re.compile(rf"^Notice \(all\): {_PLAYER} has completed their goal\.$")),
    ("release", re.compile(rf"^Notice \(all\): {_PLAYER} has released all remaining items from their world\.$")),
    ("collect", re.compile(rf"^Notice \(all\): {_PLAYER} has collected their items from other worlds\.$")),
    ("join", re.compile(rf"^Notice \(all\): {_PLAYER} \w+ (?P<game>.+) has joined\. Client\(")),
    # "left", or "stopped viewing" (text clients), "stopped tracking" (trackers), ...
    ("part", re.compile(rf"^Notice \(all\): {_PLAYER} has (?:left|stopped \w+) the game\. Client\(")),
    ("server", re.compile(r"^Notice \(all\): \[Server\]: (?P<text>.+)$")),
    (None, re.compile(r"^Notice \(all\): Cheat console: ")),
    ("hint", re.compile(r"^Notice \(Team #\d+\): \[Hint\]: (?P<receiver>.+?)'s (?P<item>.+) is at (?P<location>.+) in "
                        r"(?P<finder>.+?)'s World(?: at (?P<entrance>.+))?\. \((?P<status>[a-z ]+)\)$")),
    ("item", re.compile(r"^\(Team #\d+\) (?P<finder>.+?) sent (?P<item>.+) to (?P<receiver>.+?) "
                        r"\((?P<location>.+)\)$")),
    ("chat", re.compile(r"^Notice \(all\): (?P<player>[^:]+?): (?P<text>.+)$")),
]

_ABOUT_A_PLAYER = {"goal", "release", "collect", "join", "part"}


def parse(line: str) -> dict | None:
    """The entry for a line of the server's log, or None for a line the feed does not show."""
    for kind, rule in _RULES:
        match = rule.search(line)
        if match:
            # A chat line reads "Name: text", so the text can imitate the server's own lines; an alias may not hold
            # ": " either (see history.note_goal)
            if kind in _ABOUT_A_PLAYER and ": " in match["player"]:
                continue
            if kind is None or (kind == "chat" and match["text"].lstrip().lower().startswith("!admin")):
                return None  # remote administration is the administrators' business
            return {"kind": kind, **{k: v for k, v in match.groupdict().items() if v is not None}}
    return None


@dataclass
class _Feed:
    seq: int = 0
    entries: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP))


_FEEDS: dict[int, _Feed] = {}


def _path(game_id: int):
    return gamepaths.game_dir(game_id) / "activity.jsonl"


def _feed(game_id: int) -> _Feed:
    feed = _FEEDS.get(game_id)
    if feed is None:
        feed = _FEEDS[game_id] = _Feed()
        lines: collections.deque = collections.deque(maxlen=KEEP)
        with contextlib.suppress(OSError):
            with open(_path(game_id), encoding="utf-8") as f:
                lines.extend(f)
        for line in lines:
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
                feed.entries.append(entry)
                feed.seq = max(feed.seq, int(entry.get("seq", 0)))
    return feed


def note(game_id: int, line: str) -> dict | None:
    """Keeps a line of the server's log, if the feed shows its kind, and returns its entry."""
    entry = parse(line)
    if entry is None:
        return None
    feed = _feed(game_id)
    feed.seq += 1
    entry = {"seq": feed.seq, "at": time.time(), **entry}
    feed.entries.append(entry)
    path = _path(game_id)
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > FILE_LIMIT:
            path.replace(path.with_suffix(".jsonl.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def entries(game_id: int, after: int = 0) -> dict:
    feed = _feed(game_id)
    return {"entries": [e for e in feed.entries if e["seq"] > after], "last": feed.seq}


def forget(game_id: int) -> None:
    _FEEDS.pop(game_id, None)
