"""Chance in configs: rolling a file with weights into one without, and mystery rounds.

Rolling does what the generator does with a player file (ap_helper.py roll): weights over several values, random
ranges, triggers and a game picked at random each become one value. The config editor shows what a file could come out
as, to keep or to roll again.

A mystery round (signups.py) has one file of weights, set by whoever runs it. Players join with nothing but a player
name: each entry is that file with their name in it, so the generator rolls a game and options for every player on its
own, and nobody knows what they play before the game is generated. Changing the weights changes every entry.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import apbridge
from .config import CONFIG


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
