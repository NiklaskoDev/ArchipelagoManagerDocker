"""Reads player yaml files the way Archipelago's generator does, to learn which games and slot names they define."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

MAX_YAML_BYTES = 1024 * 1024
MAX_NAME_LENGTH = 16  # Generate.handle_name truncates slot names to this
# Aliases (*name) repeat what an anchor (&name) marks. The loader shares the repeated part, but whatever prints or copies
# it writes it out each time, so a few hundred bytes of nested aliases stand for gigabytes ("billion laughs"). A file
# may repeat itself as long as, written out, it stays this large: one per value, plus the length of each text.
MAX_EXPANDED_SIZE = 2 * MAX_YAML_BYTES


class YamlError(ValueError):
    pass


class UniqueKeyLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Same duplicate-key check as Utils.UniqueKeyLoader, which Archipelago applies to every player file.

    Parsed by libyaml where available: the pure-Python parser needs seconds for a large file.
    """

    def construct_mapping(self, node, deep=False):
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)


@dataclass
class YamlDoc:
    raw_name: str | None
    name: str | None  # the slot name the generator will produce, None if it depends on placeholders/weights
    games: list[str]
    description: str | None = None


@dataclass
class YamlInfo:
    docs: list[YamlDoc] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Things that are fine to store but make the generator fail. Reported as warnings when saving and
    # refused when generating.
    problems: list[str] = field(default_factory=list)

    @property
    def games(self) -> list[str]:
        return sorted({g for doc in self.docs for g in doc.games})

    @property
    def player_names(self) -> list[str]:
        return [doc.name for doc in self.docs if doc.name]

    @property
    def description(self) -> str | None:
        return next((doc.description for doc in self.docs if doc.description), None)


def decode_text(data: bytes) -> str:
    if len(data) > MAX_YAML_BYTES:
        raise YamlError(f"File is too large for a player yaml ({len(data) // 1024} KiB, limit {MAX_YAML_BYTES // 1024} KiB)")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise YamlError("File is not valid UTF-8 text") from None


def _weighted_keys(value: Any) -> list[str]:
    """Keys of a weighted choice mapping that can actually be rolled."""
    keys = []
    for key, weight in value.items():
        try:
            if float(weight) > 0:
                keys.append(str(key))
        except (TypeError, ValueError):
            keys.append(str(key))
    return keys


def static_name(raw: str) -> str | None:
    """The slot name for a yaml `name`, or None if placeholders make it unpredictable."""
    if any(token in raw for token in ("{", "}", "%number%", "%player%")):
        return None
    return raw.strip()[:MAX_NAME_LENGTH].strip() or None


def check_aliases(text: str) -> None:
    """Raises YamlError if aliases make the file larger than MAX_EXPANDED_SIZE once written out, or make a value contain
    itself. Reads the parser's events only, so nothing is built up on the way."""
    if "*" not in text:  # no alias without one
        return
    sizes: dict[str, int] = {}  # the written-out size of what each anchor marks
    stack: list[list] = []  # [anchor, size so far] of each mapping and list being read
    total = 0
    for event in yaml.parse(text, Loader=UniqueKeyLoader):
        if isinstance(event, yaml.CollectionStartEvent):
            stack.append([event.anchor, 1])
            continue
        if isinstance(event, yaml.ScalarEvent):
            size = 1 + len(event.value)
            if event.anchor:
                sizes[event.anchor] = size
        elif isinstance(event, yaml.AliasEvent):
            if any(anchor == event.anchor for anchor, _size in stack):
                raise YamlError(f"The alias *{event.anchor} is inside what it repeats")
            size = sizes.get(event.anchor, 1)  # an unknown one: the loader says so
        elif isinstance(event, yaml.CollectionEndEvent):
            anchor, size = stack.pop()
            if anchor:
                sizes[anchor] = size
        else:
            continue
        if stack:
            stack[-1][1] += size
        else:
            total += size
        if (stack[-1][1] if stack else total) > MAX_EXPANDED_SIZE:
            raise YamlError(f"The aliases (*name) in this file repeat too much: written out, it would be larger than "
                            f"{MAX_EXPANDED_SIZE // (1024 * 1024)} MB")


def load_documents(text: str) -> list[Any]:
    """The documents of a yaml file, as Archipelago reads them. Raises YamlError with the line of a syntax error."""
    try:
        check_aliases(text)
        return list(yaml.load_all(text.lstrip("﻿"), Loader=UniqueKeyLoader))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise YamlError(f"Invalid YAML{where}: {e.problem or e}") from None
    except yaml.YAMLError as e:
        raise YamlError(f"Invalid YAML: {e}") from None


def analyze(text: str, fallback_name: str) -> YamlInfo:
    """Parses `text`. `fallback_name` stands in for the file name stem, which is what AP uses if a yaml has no name."""
    documents = load_documents(text)

    info = YamlInfo()
    for index, doc in enumerate(documents, start=1):
        if doc is None:
            info.warnings.append(f"Document #{index} is empty and will be ignored")
            continue
        if not isinstance(doc, dict):
            raise YamlError(f"Document #{index} must be a mapping of options, not a {type(doc).__name__}")
        if "meta_description" in doc and "game" not in doc:
            raise YamlError("This looks like a meta.yaml, not a player file. Use the meta.yaml field when generating.")

        game = doc.get("game")
        if isinstance(game, str):
            games = [game.strip()]
        elif isinstance(game, dict):
            games = _weighted_keys(game)
        else:
            raise YamlError(f"Document #{index} has no 'game' set")
        if not games:
            raise YamlError(f"Document #{index}: every game in 'game' has weight 0")

        for g in games:
            if not isinstance(doc.get(g), dict):
                info.problems.append(f"Document #{index} has no '{g}:' section with that game's options "
                                     f"(an empty one, '{g}: {{}}', is fine)")

        raw_name = doc.get("name")
        name: str | None
        if isinstance(raw_name, str):
            name = static_name(raw_name)
            if len(raw_name.strip()) > MAX_NAME_LENGTH and name:
                info.warnings.append(f"Name '{raw_name.strip()}' is longer than {MAX_NAME_LENGTH} characters "
                                     f"and will be shortened to '{name}'")
        elif raw_name is None:
            name = static_name(fallback_name)
            raw_name = None
            info.warnings.append("No 'name' set; the file name will be used as the player name")
        elif isinstance(raw_name, dict):
            name, raw_name = None, ", ".join(_weighted_keys(raw_name))
        else:
            raise YamlError(f"Document #{index}: 'name' must be text")

        description = doc.get("description")
        info.docs.append(YamlDoc(
            raw_name=raw_name, name=name, games=games,
            description=description.strip()[:500] if isinstance(description, str) and description.strip() else None))

    if not info.docs:
        raise YamlError("The file contains no player settings")
    info.warnings.extend(info.problems)
    return info


_NAME_LINE = re.compile(r"^name[ \t]*:.*$", re.MULTILINE)  # only at the start of a line: a top-level key
_DOCUMENT_START = re.compile(r"\A(?:[ \t]*(?:#.*)?\n)*---[^\n]*\n")


def with_player_name(text: str, name: str) -> str:
    """The yaml of one player with the player name set to `name`. The rest of the file stays as it is, comments
    included. Raises YamlError if the name is not usable or cannot be set in this file."""
    name = name.strip()
    if static_name(name) != name or not name.isprintable():
        raise YamlError(f"A player name has 1 to {MAX_NAME_LENGTH} characters and no placeholders")
    if len([doc for doc in load_documents(text) if doc is not None]) != 1:
        raise YamlError("This file holds several players. Copy it without a new name and change the names in the "
                        "editor.")
    line = f"name: {json.dumps(name, ensure_ascii=False)}"
    if _NAME_LINE.search(text):
        result = _NAME_LINE.sub(lambda _m: line, text, count=1)
    elif start := _DOCUMENT_START.match(text):
        result = text[:start.end()] + line + "\n" + text[start.end():]
    else:
        result = line + "\n" + text
    try:
        documents = [doc for doc in load_documents(result) if doc is not None]
    except YamlError:
        documents = []
    if len(documents) != 1 or not isinstance(documents[0], dict) or documents[0].get("name") != name:
        raise YamlError("The player name could not be set in this file. Copy it without a new name and change it in "
                        "the editor.")
    return result


_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, default: str = "file") -> str:
    """A file name that is safe to use on disk and in download headers."""
    name = os.path.basename(name.replace("\\", "/"))
    name = _UNSAFE.sub("_", name).strip(" .")
    return (name or default)[:120]
