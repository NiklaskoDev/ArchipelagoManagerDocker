"""Storage of player yaml files. Content lives on disk, queryable metadata in the database."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from . import db, yamlinfo
from .config import CONFIG


def config_path(user_id: int, config_id: int) -> Path:
    return CONFIG.configs_dir / str(user_id) / f"{config_id}.yaml"


def normalize_name(name: str) -> str:
    """A config's name: a safe file name ending in .yaml. One without a letter or digit (★★★.yaml, say) is config.yaml."""
    base = os.path.basename(name.replace("\\", "/"))
    ext = next((e for e in (".yaml", ".yml") if base.lower().endswith(e)), "")
    stem = base[:len(base) - len(ext)]
    return yamlinfo.safe_filename(stem, "config") + (base[len(stem):] if ext else ".yaml")


def unique_name(user_id: int, name: str, ignore_id: int | None = None) -> str:
    taken = {r["name"].lower() for r in db.query("SELECT name FROM configs WHERE user_id = ? AND id != ?",
                                                 (user_id, ignore_id or 0))}
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    counter = 2
    while f"{stem} ({counter}){ext}".lower() in taken:
        counter += 1
    return f"{stem} ({counter}){ext}"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def analyze_bytes(data: bytes, name: str) -> tuple[str, yamlinfo.YamlInfo]:
    """Raises yamlinfo.YamlError if the data is not a usable player yaml."""
    text = yamlinfo.decode_text(data)
    return text, yamlinfo.analyze(text, fallback_name=Path(name).stem)


def store_config(user_id: int, name: str, data: bytes, *, source_game_id: int | None = None) -> sqlite3.Row:
    """Saves a new yaml for a user. Raises yamlinfo.YamlError if it is not a usable player yaml."""
    name = normalize_name(name)
    _text, info = analyze_bytes(data, name)
    name = unique_name(user_id, name)
    now = db.now()
    config_id = db.execute(
        "INSERT INTO configs(user_id, name, player_names, games, description, sha256, size, source_game_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, name, db.jdumps(info.player_names), db.jdumps(info.games), info.description,
         hashlib.sha256(data).hexdigest(), len(data), source_game_id, now, now))
    write_atomic(config_path(user_id, config_id), data)
    return db.query_one("SELECT * FROM configs WHERE id = ?", (config_id,))


def has_identical_config(user_id: int, data: bytes) -> bool:
    return db.query_one("SELECT 1 FROM configs WHERE user_id = ? AND sha256 = ?",
                        (user_id, hashlib.sha256(data).hexdigest())) is not None
