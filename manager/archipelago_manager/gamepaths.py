"""Where a hosted game keeps its files."""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG


def game_dir(game_id: int) -> Path:
    return CONFIG.games_dir / str(game_id)


def files_dir(game_id: int) -> Path:
    return game_dir(game_id) / "files"


def multidata_path(game_id: int) -> Path:
    return game_dir(game_id) / "multidata.archipelago"


def save_path(game_id: int) -> Path:
    # MultiServer ignores --savefile (in 0.6.x it is parsed but never used) and always saves next to the multidata.
    return multidata_path(game_id).with_suffix(".apsave")


def log_path(game_id: int) -> Path:
    return game_dir(game_id) / "server.log"
