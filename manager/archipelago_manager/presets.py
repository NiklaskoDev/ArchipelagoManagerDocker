"""Templates of generation options: what a generation or a sign-up round is set up with, saved under a name to be
filled in again later, such as a race or an async setup.

A template holds the generation options (spoiler log, race mode, progression balancing, plando, whether the server
starts afterwards), a meta.yaml and the Archipelago version, and optionally the server settings the new game gets
instead of the defaults. Those are only for organizers and administrators: they set up the games they generate
anyway, while a player who generates does not run the game. Every template belongs to whoever saved it; organizers and
administrators may share theirs with everyone who generates.
"""
from __future__ import annotations

from typing import Any

from . import settings as app_settings, versions
from .generation import PLANDO_OPTIONS, JobRequestError, clean_meta

MAX_PER_USER = 50
MAX_META = 65536

DEFAULTS: dict[str, Any] = {
    "spoiler": 1, "race": False, "skip_prog_balancing": False, "plando": ["bosses", "connections", "texts"],
    "host_after": True, "meta": None, "ap_version": None,
    "server_options": None,  # None: the server's defaults for new games
}


class PresetError(ValueError):
    pass


def clean_options(options: dict | None, *, organizer: bool) -> dict:
    """A template's options, complete and checked. Raises PresetError."""
    result = dict(DEFAULTS)
    for key, value in (options or {}).items():
        if key not in DEFAULTS:
            raise PresetError(f"Unknown option: {key}")
        result[key] = value
    if isinstance(result["spoiler"], bool) or result["spoiler"] not in (0, 1, 2, 3):
        raise PresetError("Spoiler level must be between 0 and 3")
    for key in ("race", "skip_prog_balancing", "host_after"):
        if not isinstance(result[key], bool):
            raise PresetError(f"{key} must be true or false")
    if not isinstance(result["plando"], list) or any(p not in PLANDO_OPTIONS for p in result["plando"]):
        raise PresetError("Unknown plando option")
    result["plando"] = [p for p in PLANDO_OPTIONS if p in result["plando"]]
    meta = result["meta"]
    if meta is not None and (not isinstance(meta, str) or len(meta) > MAX_META):
        raise PresetError("The meta.yaml is too long")
    try:
        result["meta"] = clean_meta(meta)  # checked now, rather than when a generation uses the template
    except JobRequestError as e:
        raise PresetError(e.message) from None
    version = result["ap_version"]
    if version is not None and (not isinstance(version, str) or not versions.VERSION.match(version.strip())):
        raise PresetError("The Archipelago version must look like 0.6.2")
    result["ap_version"] = version.strip() if version else None
    if result["server_options"] is not None:
        if not organizer:
            raise PresetError("Only organizers and administrators set up the games they generate")
        if not isinstance(result["server_options"], dict):
            raise PresetError("The settings for the new game are not valid")
        try:
            result["server_options"] = app_settings.clean_server_options(
                result["server_options"], base=app_settings.get_setting("default_server_options"))
        except app_settings.SettingsError as e:
            raise PresetError(str(e)) from None
    return result


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 60:
        raise PresetError("Give the template a name of up to 60 characters, such as Race or Async")
    return name
