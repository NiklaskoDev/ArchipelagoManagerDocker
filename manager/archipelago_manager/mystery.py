"""Chance in configs: rolling a file with weights into one without, and mystery rounds.

Rolling does what the generator does with a player file (ap_helper.py roll): weights over several values, random
ranges, triggers and a game picked at random each become one value. The config editor shows what a file could come out
as, to keep or to roll again.

A mystery round (signups.py) has one file of weights, set by whoever runs it. Players join with nothing but a player
name: each entry is that file with their name in it, so the generator rolls a game and options for every player on its
own, and nobody knows what they play before the game is generated. Changing the weights changes every entry. The
weights can be written by hand, or put together from a list of games (weights_for): each with how often it comes up,
and with its default options or with every option that can be left to chance rolled.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from . import apbridge, yamlform
from .config import CONFIG

MAX_GAMES = 200
MAX_WEIGHT = 1000
# Options "everything at random" leaves alone: they decide whether a seed can be finished and whether one player's death
# kills the others, which nobody should find out by chance
KEEP_DEFAULT = {"progression_balancing", "accessibility", "death_link"}


class RollError(ValueError):
    pass


async def roll(text: str) -> dict:
    """The file rolled once: {"yaml": the file without chance in it, "rolled": [{"name", "game"}, ...]}."""
    with tempfile.TemporaryDirectory(dir=CONFIG.tmp_dir) as tmp:
        path = Path(tmp) / "weights.yaml"
        path.write_text(text, encoding="utf-8")
        try:
            result = await apbridge.run_helper("roll", str(path), timeout=180)
        except apbridge.APBridgeError as e:
            message = str(e).split(": ", 1)[1] if str(e).startswith(("Exception: ", "OptionError: ", "ValueError: ")) \
                else str(e)
            raise RollError(message) from None
    return {"yaml": result["yaml"], "rolled": result["rolled"]}


class WeightsError(ValueError):
    def __init__(self, message: str, missing_games: list[str] | None = None) -> None:
        super().__init__(message)
        self.message, self.missing_games = message, missing_games or []


async def weights_for(picks: list[dict]) -> str:
    """A file of weights from a list of games: [{"game", "weight", "random": every option that can be left to chance
    is}], in that order."""
    if not picks:
        raise WeightsError("Pick at least one game")
    if len(picks) > MAX_GAMES:
        raise WeightsError(f"Pick at most {MAX_GAMES} games")
    games = [p["game"] for p in picks]
    if len(set(games)) != len(games):
        raise WeightsError("Each game can be picked once")
    if any(not isinstance(p["weight"], int) or isinstance(p["weight"], bool) or not 0 <= p["weight"] <= MAX_WEIGHT
           for p in picks):
        raise WeightsError(f"A game's weight is a whole number from 0 to {MAX_WEIGHT}")
    if not any(p["weight"] for p in picks):
        raise WeightsError("At least one game needs a weight above 0")
    missing = apbridge.missing_games(games)
    if missing:
        raise WeightsError(f"Not installed on this server: {', '.join(missing)}", missing)
    sections = {}
    for pick in picks:
        options = {}
        if pick["random"]:
            try:
                path = await apbridge.options_form_path(pick["game"])
            except apbridge.APBridgeError as e:
                raise WeightsError(str(e)) from None
            if path is None or not path.is_file():
                raise WeightsError(f"The options of {pick['game']} are not known on this server", [pick["game"]])
            schema = await asyncio.to_thread(yamlform.load_schema, path)
            options = {key: "random" for key, field in yamlform.fields(schema).items()
                       if field.get("random") and key not in KEEP_DEFAULT and "death_link" not in key}
        sections[pick["game"]] = options
    document = {"name": "Player", "description": "Mystery", "game": {p["game"]: p["weight"] for p in picks}, **sections}
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)
