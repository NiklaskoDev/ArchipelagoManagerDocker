"""Checking a config the way the generator reads it, without generating: the Check button on My configs and in the
config editor.

Two parts. What the manager sees itself: the file's structure (yamlinfo), player names the generator refuses, and games
that need their base ROM on the server. Then what Archipelago says when it reads the file (ap_helper.py check): option
values it does not take, item and location names it does not know, games that are not installed, option names it
would ignore. Archipelago's part runs only when the manager's finds nothing that would stop it anyway.

Nothing is generated, so what only shows when a seed is made (a world that cannot place its items with these options,
say) is not found here; a generation finds that.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from . import apbridge, yamlinfo
from .config import CONFIG

# Every check loads Archipelago with all its games in a helper. Checks take at most this many of the helpers' places at
# a time (apbridge.run_helper), so players checking away cannot hold up imports, trackers and the like.
_CHECKS = asyncio.Semaphore(2)
# what Archipelago puts in front of its messages, which says nothing to a player
_PREFIXES = ("Exception: ", "OptionError: ", "ValueError: ", "KeyError: ")


async def check(text: str) -> dict:
    """{"ok": whether nothing would stop the generator, "errors" and "warnings" about the whole file, "players": [{"name",
    "games", "errors", "warnings"}] as Archipelago reads them, "missing_roms": games whose base ROM is not on the server}.
    Raises apbridge.APBridgeError if Archipelago cannot be asked."""
    errors: list[str] = []
    try:
        info = await asyncio.to_thread(yamlinfo.analyze, text, "config")
    except yamlinfo.YamlError as e:
        return _result([str(e)], [], [], [])
    errors += info.problems
    warnings = [w for w in info.warnings if w not in info.problems]
    seen: set[str] = set()
    for name in info.player_names:
        if name.casefold() == "archipelago":
            errors.append("A player cannot be named Archipelago")
        elif name.casefold() in seen:
            errors.append(f"Two players in this file are named '{name}'. Player names must be unique.")
        seen.add(name.casefold())
    missing_roms = [item["game"] for item in apbridge.missing_files(info.games)]
    if errors:  # Archipelago would only stop at the same thing, less clearly
        return _result(errors, warnings, [], missing_roms)

    with tempfile.TemporaryDirectory(dir=CONFIG.tmp_dir) as tmp:
        path = Path(tmp) / "player.yaml"
        path.write_text(text, encoding="utf-8")
        try:
            async with _CHECKS:
                players = (await apbridge.run_helper("check", str(path), timeout=180))["players"]
        except apbridge.APBridgeError as e:
            message = str(e)
            if message.startswith(_PREFIXES):  # the file itself: Archipelago could not read it
                return _result([message.split(": ", 1)[1]], warnings, [], missing_roms)
            raise
    return _result(errors, warnings, players, missing_roms)


def _result(errors: list[str], warnings: list[str], players: list[dict], missing_roms: list[str]) -> dict:
    return {"ok": not errors and not any(p["errors"] for p in players), "errors": errors, "warnings": warnings,
            "players": players, "missing_roms": missing_roms}
